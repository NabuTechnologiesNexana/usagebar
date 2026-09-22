#!/usr/bin/env python3
"""Usage collector for UsageBar. Prints one JSON object to stdout:

    {"gauges": [{"id", "short", "label", "percent", "reset", "group"}], "errors": [...]}

Sources (each is skipped silently when its credentials are not on this Mac):
  claude  - the endpoint Claude Code's /usage uses. OAuth token from the Keychain,
            sent only to api.anthropic.com. 5h session, weekly all-models, weekly per-model.
  gemini  - Antigravity quota summary (5h + weekly per model group, shared by the `agy`
            CLI and the IDE). Read from a running agy/Antigravity language server on
            localhost when there is one, else from the Cloud Code API with agy's Keychain
            token (refreshed with agy's own OAuth client, read from the agy binary).
  codex   - OpenAI Codex CLI rate limits (ChatGPT login) from ~/.codex/auth.json, via the
            endpoint CodexBar uses. Not yet exercised against a live account.

Sends one macOS notification per limit per reset window when it crosses NOTIFY_AT.
Runs on the system /usr/bin/python3 (3.9) - keep it stdlib-only and 3.9-compatible.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("USAGEBAR_STATE", Path.home() / "Library/Application Support/UsageBar"))
NOTIFY_AT = int(os.environ.get("USAGEBAR_NOTIFY_AT", "80"))
NOTIFIED_FILE = STATE_DIR / "notified.json"
GEMINI_TOKEN_CACHE = STATE_DIR / "gemini_token.json"
ANTIGRAVITY_CACHE = STATE_DIR / "antigravity_cache.json"
ANTIGRAVITY_CSRF_CACHE = STATE_DIR / "antigravity_csrf.json"
ANTIGRAVITY_CACHE_TTL = 120
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


def gauge(id_: str, short: str, label: str, percent: float, reset: str | None, group: str) -> dict:
    return {"id": id_, "short": short, "label": label, "percent": int(round(percent)), "reset": reset, "group": group}


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
        out.append(gauge(f"claude:{kind}", short, label, lim["percent"], lim["resets_at"], "claude"))
    return out


# --- Gemini / Antigravity ----------------------------------------------------
# Same data the Antigravity IDE shows: per model-group 5h + weekly windows.
# 1. Any running agy / Antigravity language server answers on localhost without
#    auth - preferred, nothing leaves the machine.
# 2. Otherwise the Cloud Code summary endpoint with agy's token (identifying as
#    the Antigravity client, the way CodexBar does it).

ANTIGRAVITY_RPC = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
ANTIGRAVITY_GROUPS = {"Gemini Models": "Gemini", "Claude and GPT models": "Other models"}


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
    if csrf:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ANTIGRAVITY_CSRF_CACHE.write_text(json.dumps({"csrf": csrf}))
            ANTIGRAVITY_CSRF_CACHE.chmod(0o600)
        except Exception:
            pass
    elif ANTIGRAVITY_CSRF_CACHE.exists():
        try:
            csrf = json.loads(ANTIGRAVITY_CSRF_CACHE.read_text()).get("csrf")
        except Exception:
            csrf = None

    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["x-codeium-csrf-token"] = csrf

    ports = antigravity_local_ports()
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    if ls_addr and ":" in ls_addr:
        try:
            p = int(ls_addr.rsplit(":", 1)[1])
            if p not in ports:
                ports.insert(0, p)
        except ValueError:
            pass

    for port in ports:
        for scheme in ("http", "https"):
            try:
                req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}{ANTIGRAVITY_RPC}", data=b"{}",
                                             headers=headers)
                with urllib.request.urlopen(req, timeout=2, context=ssl._create_unverified_context()) as r:
                    data = json.load(r)
                    if "response" in data:
                        return data["response"]
            except Exception:
                continue
    return None


def antigravity_cli_summary() -> dict | None:
    """Use `agy -p /usage --output-format json` (supported in agy 1.1.11+).
    Works reliably even when the local server enforces CSRF tokens. Cached for ANTIGRAVITY_CACHE_TTL seconds."""
    now = datetime.now(timezone.utc).timestamp()
    if ANTIGRAVITY_CACHE.exists():
        try:
            cached = json.loads(ANTIGRAVITY_CACHE.read_text())
            if now - cached.get("timestamp", 0) < ANTIGRAVITY_CACHE_TTL:
                return cached.get("data")
        except Exception:
            pass

    agy = find_binary("agy")
    if not agy:
        return None

    try:
        r = subprocess.run([agy, "-p", "/usage", "--output-format", "json"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        summary = data.get("command", {}).get("data")
        if summary and "groups" in summary:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                ANTIGRAVITY_CACHE.write_text(json.dumps({"timestamp": now, "data": summary}))
                ANTIGRAVITY_CACHE.chmod(0o600)
            except Exception:
                pass
            return summary
    except Exception:
        return None
    return None


def gemini_oauth_clients() -> list[tuple[str, str]]:
    """OAuth client id/secret pairs embedded in the agy binary (public desktop-app credentials)."""
    agy = find_binary("agy")
    if not agy:
        return []
    blob = Path(agy).read_bytes()
    ids = sorted(set(re.findall(rb"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", blob)))
    secrets = sorted(set(re.findall(rb"GOCSPX-[A-Za-z0-9_-]{28}", blob)))
    return [(i.decode(), s.decode()) for i in ids for s in secrets]


def gemini_token() -> str | None:
    raw = keychain("gemini", "antigravity")
    if not raw:
        return None
    stored = json.loads(base64.b64decode(raw[len("go-keyring-base64:"):] if raw.startswith("go-keyring-base64:") else raw))["token"]
    now = datetime.now(timezone.utc)
    cached = json.loads(GEMINI_TOKEN_CACHE.read_text()) if GEMINI_TOKEN_CACHE.exists() else {}
    for tok in (cached, stored):
        if tok.get("expiry") and parse_iso(tok["expiry"]) - now > timedelta(minutes=2):
            return tok["access_token"]
    pairs = gemini_oauth_clients()
    if cached.get("client"):
        pairs.sort(key=lambda p: p != tuple(cached["client"]))  # try the pair that worked last time first
    for client_id, client_secret in pairs:
        form = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                       "refresh_token": stored["refresh_token"], "grant_type": "refresh_token"}).encode()
        try:
            fresh = http_json("https://oauth2.googleapis.com/token", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        except urllib.error.HTTPError:
            continue
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        GEMINI_TOKEN_CACHE.write_text(json.dumps({
            "access_token": fresh["access_token"],
            "expiry": (now + timedelta(seconds=fresh["expires_in"])).isoformat(),
            "client": [client_id, client_secret],
        }))
        GEMINI_TOKEN_CACHE.chmod(0o600)
        return fresh["access_token"]
    raise RuntimeError("Gemini token expired and refresh failed - run `agy` once to log in again")


def antigravity_cloud_summary() -> dict | None:
    token = gemini_token()
    if not token:
        return None
    return http_json("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                     {"Authorization": f"Bearer {token}", "User-Agent": "antigravity"}, b"{}")


def gemini_gauges() -> list[dict]:
    summary = antigravity_local_summary() or antigravity_cli_summary() or antigravity_cloud_summary()
    if not summary:
        return []
    out = []
    for group in summary.get("groups", []):
        group_name = group.get("displayName") or group.get("name") or ""
        name = ANTIGRAVITY_GROUPS.get(group_name, group_name)
        for b in sorted(group.get("buckets", []), key=lambda b: b.get("window") != "5h"):  # 5h row first
            window = "5 h" if b.get("window") == "5h" else "týždeň"
            bid = b.get("bucketId") or b.get("id") or ""
            rem = b.get("remainingFraction") if "remainingFraction" in b else b.get("remaining_fraction", 1)
            reset = b.get("resetTime") or b.get("reset_time")
            out.append(gauge(f"antigravity:{bid}", "G" if name == "Gemini" else "A", f"{name} · {window}",
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
    out = []
    for key in ("primary_window", "secondary_window"):
        win = (data.get("rate_limit") or {}).get(key)
        if not win:
            continue
        seconds = win.get("limit_window_seconds") or 0
        window = f"{round(seconds / 3600)} h" if seconds and seconds < 48 * 3600 else "týždeň"
        reset = win.get("reset_at")
        if isinstance(reset, (int, float)):
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        out.append(gauge(f"codex:{key}", "X", f"Codex · {window}", win.get("used_percent", 0), reset, "codex"))
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
    for source in SOURCES:
        try:
            gauges += source()
        except Exception as e:  # keep the bar alive; surface the reason in the menu
            errors.append(f"{source.__name__.split('_')[0]}: {type(e).__name__}: {str(e)[:80]}")
    try:
        notify(gauges)
    except Exception as e:
        errors.append(f"notify: {e}")
    print(json.dumps({"gauges": gauges, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
