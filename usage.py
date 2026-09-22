#!/usr/bin/env python3
"""Usage collector for UsageBar. Prints one JSON object to stdout:

    {"gauges": [{"id", "short", "label", "percent", "reset", "group"}], "errors": [...]}

Sources (each is skipped silently when its credentials are not on this Mac):
  claude  - the endpoint Claude Code's /usage uses. OAuth token from the Keychain,
            sent only to api.anthropic.com. 5h session, weekly all-models, weekly per-model.
  gemini  - Antigravity quota summary (5h + weekly per model group, shared by the `agy`
            CLI and the IDE): `agy -p /usage --output-format json`, cached briefly. A running
            language server on localhost is tried first when a CSRF token is provided.
  codex   - OpenAI Codex CLI rate limits (ChatGPT login) from ~/.codex/auth.json, via the
            endpoint CodexBar uses.

When a source fails, its last good gauges are re-emitted with "stale": true (plus the error),
so the bar keeps showing real numbers instead of dropping the section or inventing 0 %.
Sends one macOS notification per limit per reset window when it crosses NOTIFY_AT.
Runs on the system /usr/bin/python3 (3.9) - keep it stdlib-only and 3.9-compatible.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("USAGEBAR_STATE", Path.home() / "Library/Application Support/UsageBar"))
NOTIFY_AT = int(os.environ.get("USAGEBAR_NOTIFY_AT", "80"))
NOTIFIED_FILE = STATE_DIR / "notified.json"
LAST_OK_FILE = STATE_DIR / "last_ok.json"
ANTIGRAVITY_CACHE = STATE_DIR / "antigravity_cache.json"
ANTIGRAVITY_CACHE_TTL = 120  # a manual "Obnoviť" right after the timer should not spawn agy again
ANTIGRAVITY_TIMEOUT = 40  # agy -p /usage takes 4-6 s warm, ~15 s cold
CODEX_AUTH = Path.home() / ".codex/auth.json"


# --- helpers -----------------------------------------------------------------

def parse_iso(value: str) -> datetime:
    """Python 3.9 fromisoformat rejects 'Z' and fractions that are not 3/6 digits."""
    value = value.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", value)
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")


def find_binary(name: str) -> str | None:
    """launchd gives the app a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so PATH lookup
    alone misses Homebrew and ~/.local/bin installs - search the usual places explicitly."""
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/opt/homebrew/bin", "/usr/local/bin", Path.home() / ".local/bin",
                      Path.home() / "bin", "/usr/sbin", "/sbin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def keychain(service: str, account: str | None = None) -> str | None:
    cmd = ["security", "find-generic-password", "-s", service, *(["-a", account] if account else []), "-w"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


def http_json(url: str, headers: dict, body: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def write_private(path: Path, payload: dict) -> None:
    """0600 from the first byte (chmod after write_text leaves a window with default perms)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gauge(id_: str, short: str, label: str, percent: float, reset: str | None, group: str) -> dict:
    return {"id": id_, "short": short, "label": label, "percent": int(round(percent)), "reset": reset, "group": group,
            "fetched_at": now_iso(), "stale": False}


# --- Claude ------------------------------------------------------------------

def claude_gauges() -> list[dict]:
    raw = keychain("Claude Code-credentials")
    if not raw:
        return []
    token = json.loads(raw)["claudeAiOauth"]["accessToken"]
    data = http_json("https://api.anthropic.com/api/oauth/usage",
                     {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    out = []
    for lim in data["limits"]:
        kind = lim["kind"]
        if kind == "session":
            short, label = "C", "Claude · 5 h"
        elif kind == "weekly_all":
            short, label = "W", "Claude · týždeň"
        elif kind == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name", "?")
            short, label = model[:1], f"Claude · týždeň {model}"
        else:
            continue
        id_ = f"claude:{kind}:{model}" if kind == "weekly_scoped" else f"claude:{kind}"  # scoped gauges must not share a notification key
        out.append(gauge(id_, short, label, lim["percent"], lim["resets_at"], "claude"))
    return out


# --- Gemini / Antigravity ----------------------------------------------------
# Same data the Antigravity IDE shows: per model-group 5h + weekly windows.
# 1. A running agy / Antigravity language server on localhost, if ANTIGRAVITY_CSRF_TOKEN
#    is set (agy 1.2+ rejects RPCs without the x-codeium-csrf-token header). Off by default.
# 2. `agy -p /usage --output-format json` (agy 1.1.11+). Authoritative, 4-15 s.
# The Cloud Code endpoint (cloudcode-pa.googleapis.com retrieveUserQuotaSummary) is NOT
# used any more: it answers with remainingFraction 1 for every bucket, i.e. a fake 0 %.

ANTIGRAVITY_RPC = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
ANTIGRAVITY_GROUPS = {"Gemini Models": "Gemini", "Claude and GPT models": "Other models"}
ANTIGRAVITY_WINDOWS = {"5h": "5 h", "weekly": "týždeň"}


def antigravity_local_ports() -> list[int]:
    lsof = find_binary("lsof")
    if not lsof:
        return []
    r = subprocess.run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fcn"], capture_output=True, text=True, timeout=10)
    ports, cmd = [], ""
    for line in r.stdout.splitlines():
        if line.startswith("c"):
            cmd = line[1:].lower()
        elif line.startswith("n127.0.0.1:") and ("agy" in cmd or "language_server" in cmd or "antigravity" in cmd):
            ports.append(int(line.rsplit(":", 1)[1]))
    return sorted(set(ports))


def antigravity_local_summary() -> dict | None:
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if not csrf:
        return None
    headers = {"Content-Type": "application/json", "x-codeium-csrf-token": csrf}
    for port in antigravity_local_ports():
        for scheme in ("http", "https"):
            try:
                req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}{ANTIGRAVITY_RPC}", data=b"{}", headers=headers)
                with urllib.request.urlopen(req, timeout=2, context=ssl._create_unverified_context()) as r:
                    data = json.load(r)
                if "response" in data:
                    return data["response"]
            except Exception:
                continue
    return None


def antigravity_cli_summary() -> dict:
    now = datetime.now(timezone.utc).timestamp()
    if ANTIGRAVITY_CACHE.exists():
        try:
            cached = json.loads(ANTIGRAVITY_CACHE.read_text())
            if now - cached.get("timestamp", 0) < ANTIGRAVITY_CACHE_TTL:
                return cached["data"]
        except Exception:
            pass
    agy = find_binary("agy")
    if not agy:
        raise RuntimeError("agy nie je nainštalované")
    try:
        r = subprocess.run([agy, "-p", "/usage", "--output-format", "json"],
                           capture_output=True, text=True, timeout=ANTIGRAVITY_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"agy neodpovedalo do {ANTIGRAVITY_TIMEOUT} s")
    if r.returncode != 0:
        raise RuntimeError(f"agy skončilo s kódom {r.returncode}: {(r.stderr or r.stdout).strip()[:60]}")
    summary = json.loads(r.stdout).get("command", {}).get("data")
    antigravity_buckets(summary)  # validate before caching, so a broken shape is never served from cache
    try:
        write_private(ANTIGRAVITY_CACHE, {"timestamp": datetime.now(timezone.utc).timestamp(), "data": summary})
    except Exception:
        pass  # cache is an optimisation, never a reason to lose a good sample
    return summary


def antigravity_buckets(summary: dict | None) -> list[tuple[str, str, str, float, str | None]]:
    """(group name, bucket id, window key, remaining fraction, reset) - raises on anything unexpected,
    so a renamed field surfaces as a ⚠ row instead of a silent 0 %."""
    groups = (summary or {}).get("groups")
    if not groups:
        raise ValueError("odpoveď bez groups")
    out = []
    for group in groups:
        name = ANTIGRAVITY_GROUPS.get(group.get("displayName") or group.get("name") or "", None)
        if name is None:
            raise ValueError(f"neznáma skupina {group.get('displayName') or group.get('name')!r}")
        buckets = group.get("buckets")
        if not buckets:
            raise ValueError(f"skupina {name} bez buckets")
        for b in buckets:
            bid = b.get("bucketId") or b.get("id")
            rem = b.get("remainingFraction", b.get("remaining_fraction"))
            window = b.get("window")
            if not bid or not isinstance(rem, (int, float)) or not 0 <= rem <= 1 or window not in ANTIGRAVITY_WINDOWS:
                raise ValueError(f"neplatný bucket {bid!r}: rem={rem!r} window={window!r}")
            out.append((name, bid, window, float(rem), b.get("resetTime") or b.get("reset_time")))
    return out


def gemini_gauges() -> list[dict]:
    summary = antigravity_local_summary() or antigravity_cli_summary()
    out = []
    rows = sorted(antigravity_buckets(summary), key=lambda t: (t[0] != "Gemini", t[2] != "5h"))  # Gemini group first, 5h first
    for name, bid, window, rem, reset in rows:
        out.append(gauge(f"antigravity:{bid}", "G" if name == "Gemini" else "A", f"{name} · {ANTIGRAVITY_WINDOWS[window]}",
                         (1 - rem) * 100, reset, "gemini"))
    return out


# --- Codex ---------------------------------------------------------------------
# Endpoint and response shape taken from CodexBar's CodexOAuthUsageFetcher.swift
# (GET backend-api/wham/usage -> rate_limit.primary_window/secondary_window with
# used_percent, reset_at epoch, limit_window_seconds). Not exercised against a live
# Codex account yet - if it breaks, the row shows as a warning in the menu.

def codex_gauges() -> list[dict]:
    if not CODEX_AUTH.exists() or not find_binary("codex"):  # stale auth.json without the CLI is common
        return []
    tokens = json.loads(CODEX_AUTH.read_text()).get("tokens") or {}
    if not tokens.get("access_token"):
        return []
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "User-Agent": "UsageBar"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    rate = data.get("rate_limit")
    if not isinstance(rate, dict):
        raise RuntimeError("odpoveď bez rate_limit (zmenilo sa API?)")
    out = []
    for key in ("primary_window", "secondary_window"):
        win = rate.get(key)
        if not win:
            continue
        used = win.get("used_percent")
        if not isinstance(used, (int, float)):
            raise RuntimeError(f"{key} bez used_percent")
        seconds = win.get("limit_window_seconds") or 0
        window = f"{round(seconds / 3600)} h" if seconds and seconds < 48 * 3600 else "týždeň"
        reset = win.get("reset_at")
        if isinstance(reset, (int, float)):
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        out.append(gauge(f"codex:{key}", "X", f"Codex · {window}", used, reset, "codex"))
    if not out:
        raise RuntimeError("rate_limit bez okien (účet bez limitov?)")
    return out


# --- notifications ------------------------------------------------------------

def notify(gauges: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen = json.loads(NOTIFIED_FILE.read_text()) if NOTIFIED_FILE.exists() else {}
    now_key = datetime.now(timezone.utc).isoformat()[:16]
    for g in gauges:
        key = f"{g['id']}@{(g['reset'] or '')[:16]}"  # one notification per limit per reset window (minute precision)
        if g["percent"] >= NOTIFY_AT and key not in seen:
            msg = f"{g['label']}: {g['percent']} %"
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "UsageBar" sound name "Glass"'], timeout=10)
            seen[key] = True
    NOTIFIED_FILE.write_text(json.dumps({k: v for k, v in seen.items() if k.split("@", 1)[1] > now_key}))


SOURCES = [claude_gauges, gemini_gauges, codex_gauges]


def main() -> None:
    gauges, errors = [], []
    try:
        last_ok = json.loads(LAST_OK_FILE.read_text()) if LAST_OK_FILE.exists() else {}
    except Exception:
        last_ok = {}
    for source in SOURCES:
        name = source.__name__.split("_")[0]
        try:
            fresh = source()
            gauges += fresh
            if fresh:
                last_ok[name] = fresh
            else:
                last_ok.pop(name, None)  # source deliberately absent (no login) - forget it
        except Exception as e:  # keep the bar alive: last good numbers greyed out + the reason in the menu
            why = str(e)[:80] if isinstance(e, (RuntimeError, ValueError)) else f"{type(e).__name__}: {str(e)[:70]}"
            errors.append(f"{name}: {why}")
            gauges += [{**g, "stale": True} for g in last_ok.get(name, [])]
    try:
        write_private(LAST_OK_FILE, last_ok)
    except Exception:
        pass
    try:
        notify([g for g in gauges if not g["stale"]])
    except Exception as e:
        errors.append(f"notify: {e}")
    print(json.dumps({"gauges": gauges, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
