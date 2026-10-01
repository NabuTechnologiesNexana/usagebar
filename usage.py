#!/usr/bin/env python3
"""Usage collector for UsageBar.

The menubar runs `python3 usage.py` with no arguments. That prints one JSON object:

    {"gauges": [{"id", "short", "label", "percent", "reset", "group"}], "errors": [...]}

Invoked as `usagebar`, it prints every window for every signed-in account.
`usagebar antigravity|grok|codex|claude` prints that account. A sample newer
than the menu-bar refresh is reused, so the command does not wait on the network
for numbers the menu already has. This path does not post the macOS notification.

Sources (each is skipped silently when its credentials are not on this Mac):
  claude  - the endpoint Claude Code's /usage uses. OAuth token from the Keychain,
            sent only to api.anthropic.com. 5h session, weekly all-models, weekly per-model.
  gemini  - Antigravity quota summary (5h + weekly per model group, shared by the `agy`
            CLI and the IDE): `agy -p /usage --output-format json`, cached briefly. A running
            language server on localhost is tried first when a CSRF token is provided.
  codex   - OpenAI Codex CLI rate limits (ChatGPT login) from ~/.codex/auth.json, via the
            endpoint CodexBar uses.
  grok    - Grok Build subscription limits, the same numbers /usage shows in the CLI.
            OAuth access token from ~/.grok/auth.json (never written by this tool),
            sent only to cli-chat-proxy.grok.com. Weekly credit window, and the monthly
            allowance when the account has one.

When a source fails, its last good gauges are re-emitted with "stale": true (plus the error),
so the bar keeps showing real numbers instead of dropping the section or inventing 0 %.
Sends one macOS notification per limit per reset window when it crosses NOTIFY_AT.
Runs on the system /usr/bin/python3 (3.9) - keep it stdlib-only and 3.9-compatible.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
GROK_AUTH = Path.home() / ".grok/auth.json"
GROK_VERSION = Path.home() / ".grok/version.json"
GROK_PROXY = os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL", "https://cli-chat-proxy.grok.com/v1").rstrip("/")


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



# --- Grok ---------------------------------------------------------------------
# Same numbers the Grok CLI /usage modal shows. The weekly credit percent comes from
# GET {cli-chat-proxy}/billing?format=credits; the monthly dollar allowance from
# GET {cli-chat-proxy}/billing. Both want the OIDC access token stored at
# ~/.grok/auth.json under "key", plus the CLI identity headers.
# This file is only read. A token inside 2 minutes of expiry is renewed by
# `grok models`, which is the CLI's own refresh — refresh-token rotation stays
# inside grok, so a collector can never invalidate the login.
# An API-key login has no subscription windows and is skipped, same as Codex.

class GrokAuthExpired(RuntimeError):
    pass


def grok_client_version() -> str:
    try:
        version = json.loads(GROK_VERSION.read_text()).get("version")
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
            return version
    except Exception:
        pass
    grok = find_binary("grok")
    if not grok:
        return "0.0.0"
    try:
        r = subprocess.run([grok, "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except Exception:
        return "0.0.0"
    m = re.search(r"(\d+\.\d+\.\d+)", r.stdout or "")
    return m.group(1) if m else "0.0.0"


def grok_load_auth() -> dict:
    try:
        data = json.loads(GROK_AUTH.read_text())
    except Exception as e:
        raise RuntimeError(f"auth.json sa nedá čítať ({type(e).__name__})")
    if not isinstance(data, dict):
        raise RuntimeError("auth.json nemá očakávaný tvar")
    return data


def grok_credential(data: dict) -> dict | None:
    """The OIDC session grok login wrote. API-key entries are not a subscription."""
    best, best_exp = None, ""
    for entry in data.values():
        if not isinstance(entry, dict) or entry.get("auth_mode") != "oidc":
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        exp = entry.get("expires_at")
        exp_s = exp if isinstance(exp, str) else ""
        if best is None or exp_s > best_exp:
            best, best_exp = entry, exp_s
    return best


def grok_token_fresh(entry: dict, skew_s: int = 120) -> bool:
    exp = entry.get("expires_at")
    if not isinstance(exp, str) or not exp:
        return True  # unknown expiry: try the token, refresh only after a 401
    try:
        return parse_iso(exp) > datetime.now(timezone.utc) + timedelta(seconds=skew_s)
    except Exception:
        return True


def grok_public_text(text: str) -> str:
    text = re.sub(r"eyJ[A-Za-z0-9_\-]{8,}", "…", text)
    line = text.strip().splitlines()
    return (line[-1] if line else "")[:70]


def grok_refresh_login() -> None:
    grok = find_binary("grok")
    if not grok:
        raise RuntimeError("grok nie je nainštalované")
    try:
        r = subprocess.run([grok, "models"], capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("grok models neodpovedalo do 40 s")
    if r.returncode != 0:
        why = grok_public_text(r.stderr or r.stdout or "") or f"kód {r.returncode}"
        raise RuntimeError(f"obnova prihlásenia zlyhala: {why}")


def grok_access_token(force: bool = False) -> str | None:
    if not GROK_AUTH.exists():
        return None
    entry = grok_credential(grok_load_auth())
    if entry is None:
        return None
    if force or not grok_token_fresh(entry):
        grok_refresh_login()
        entry = grok_credential(grok_load_auth())
        # skew 0: a just-refreshed token only has to be unexpired. A 401 on a token
        # that is still in date falls through and surfaces on the retry.
        if entry is None or not grok_token_fresh(entry, skew_s=0):
            raise RuntimeError("prihlásenie Grok vypršalo, spusti grok login")
    key = entry.get("key")
    if not isinstance(key, str) or not key:
        return None
    return key


def grok_percent(value: float, what: str) -> float:
    if not 0 <= value <= 100.5:
        raise ValueError(f"{what} mimo rozsah ({value})")
    return min(100.0, value)


def grok_money(raw: object) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("val")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def grok_optional_money(config: dict, key: str) -> float | None:
    if key not in config or config[key] is None:
        return None
    value = grok_money(config[key])
    if value is None:
        raise ValueError(f"{key} má neznámy tvar")
    return value


def grok_reset(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parse_iso(value)
    except Exception:
        return None
    return value


def grok_credit_percent(config: dict) -> float | None:
    """None when this payload has no credit window. 0 when the CLI omits a weekly percent."""
    if "creditUsagePercent" in config and config["creditUsagePercent"] is not None:
        raw = config["creditUsagePercent"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("creditUsagePercent nie je číslo")
        return grok_percent(float(raw), "creditUsagePercent")
    products = config.get("productUsage")
    if products is not None and not isinstance(products, list):
        raise ValueError("productUsage nie je zoznam")
    for item in products or []:
        if not isinstance(item, dict):
            raise ValueError("productUsage má neznámy tvar")
        name = str(item.get("product") or "").lower().replace("-", "").replace("_", "")
        if name not in ("grokbuild", "build"):
            continue
        raw = item.get("usagePercent")
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("productUsage.usagePercent nie je číslo")
        return grok_percent(float(raw), "productUsage")
    period = config.get("currentPeriod")
    if isinstance(period, dict) and "WEEK" in str(period.get("type") or "").upper():
        return 0.0  # the CLI drops the field entirely while the week is still at 0 %
    return None


def grok_config(payload: dict) -> dict:
    config = payload.get("config")
    if isinstance(config, dict):
        return config
    if any(k in payload for k in ("creditUsagePercent", "monthlyLimit", "currentPeriod", "productUsage")):
        return payload
    raise RuntimeError("odpoveď bez config (zmenilo sa API?)")


def grok_fetch(token: str, query: str) -> dict:
    version = grok_client_version()
    req = urllib.request.Request(f"{GROK_PROXY}/billing{query}", headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": version,
        "x-grok-client-identifier": "grok-shell",
        "User-Agent": f"grok-cli/{version}",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GrokAuthExpired(f"billing HTTP {e.code}")
        raise RuntimeError(f"billing HTTP {e.code}")
    if not isinstance(payload, dict):
        raise RuntimeError("billing nevrátilo objekt")
    return grok_config(payload)


def grok_windows(weekly: dict, monthly: dict | None) -> list[tuple[str, str, str, float, str | None]]:
    """(id, short, label, percent, reset). Raises on a shape that would otherwise draw a fake 0 %."""
    out = []
    period = weekly.get("currentPeriod") if isinstance(weekly.get("currentPeriod"), dict) else {}
    kind = str(period.get("type") or "")
    credits_monthly = "MONTH" in kind.upper() and "WEEK" not in kind.upper()
    credit = grok_credit_percent(weekly)
    if credit is not None:
        label = "Grok · mesiac" if credits_monthly else "Grok · týždeň"
        out.append(("grok:weekly", "K", label, credit,
                    grok_reset(period.get("end")) or grok_reset(weekly.get("billingPeriodEnd"))))
    if not credits_monthly and monthly:
        limit = grok_optional_money(monthly, "monthlyLimit")
        used = grok_optional_money(monthly, "used")
        if limit is not None and limit > 0 and used is not None:
            out.append(("grok:monthly", "K", "Grok · mesiac", grok_percent(used / limit * 100, "monthly"),
                        grok_reset(monthly.get("billingPeriodEnd"))))
        elif limit is not None and limit > 0 and used is None:
            raise ValueError("monthlyLimit bez used")
    # Pay-as-you-go rides the credits payload; the monthly payload is only a fallback.
    cap = grok_optional_money(weekly, "onDemandCap")
    spent = grok_optional_money(weekly, "onDemandUsed")
    reset_source = weekly
    if cap is None and monthly:
        cap = grok_optional_money(monthly, "onDemandCap")
        spent = grok_optional_money(monthly, "onDemandUsed")
        reset_source = monthly
    if cap is not None and cap > 0 and spent is not None:
        reset = grok_reset(reset_source.get("billingPeriodEnd")) or grok_reset(period.get("end"))
        out.append(("grok:ondemand", "K", "Grok · pay-as-you-go", grok_percent(spent / cap * 100, "onDemand"), reset))
    elif cap is not None and cap > 0 and spent is None:
        raise ValueError("onDemandCap bez onDemandUsed")
    if not out:
        raise RuntimeError("billing bez okien (účet bez limitov?)")
    return out


def grok_gauges() -> list[dict]:
    if not find_binary("grok") or not GROK_AUTH.exists():
        return []
    token = grok_access_token()
    if not token:
        return []
    def pair(tok: str) -> tuple[dict, dict | None]:
        weekly_cfg = grok_fetch(tok, "?format=credits")
        try:
            monthly_cfg = grok_fetch(tok, "")
        except GrokAuthExpired:
            raise
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
            monthly_cfg = None  # a plan with only the weekly credit window
        return weekly_cfg, monthly_cfg

    try:
        weekly, monthly = pair(token)
    except GrokAuthExpired:
        token = grok_access_token(force=True)
        if not token:
            raise RuntimeError("prihlásenie Grok vypršalo, spusti grok login")
        weekly, monthly = pair(token)
    return [gauge(*row, "grok") for row in grok_windows(weekly, monthly)]


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


# Internal keys match the gauge "group" and the last_ok file. The CLI says
# "antigravity" for the gemini group, which is the name typed on the command line.
SOURCE_FUNCS = {
    "claude": claude_gauges,
    "gemini": gemini_gauges,
    "codex": codex_gauges,
    "grok": grok_gauges,
}
CLI_SOURCES = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "antigravity": "gemini",
    "gemini": "gemini",
    "agy": "gemini",
}
SOURCE_NAME = {
    "claude": "claude",
    "gemini": "antigravity",
    "codex": "codex",
    "grok": "grok",
}
_WINDOW_WORDS = (("týždeň", "week"), ("mesiac", "month"), ("5 h", "5h"))


CLI_MAX_AGE = 300  # the menu refreshes on this cadence; reuse a sample that young


def _source_error(exc: Exception) -> str:
    return str(exc)[:80] if isinstance(exc, (RuntimeError, ValueError)) else f"{type(exc).__name__}: {str(exc)[:70]}"


def _sample_age(gauges: list[dict]) -> float | None:
    now = datetime.now(timezone.utc)
    ages = []
    for g in gauges:
        raw = g.get("fetched_at")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            ages.append((now - parse_iso(raw)).total_seconds())
        except Exception:
            return None
    return max(ages) if ages else None


def _fetch_one(name: str) -> tuple[str, list[dict] | None, str | None]:
    try:
        return name, SOURCE_FUNCS[name](), None
    except Exception as exc:
        return name, None, _source_error(exc)


def _fetch(names: list[str], last_ok: dict, parallel: bool) -> tuple[list[dict], list[str]]:
    if parallel and len(names) > 1:
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(_fetch_one, names))
    else:
        results = [_fetch_one(name) for name in names]
    by_name = {name: (fresh, err) for name, fresh, err in results}
    gauges: list[dict] = []
    errors: list[str] = []
    for name in names:
        fresh, err = by_name[name]
        if err is not None:
            errors.append(f"{name}: {err}")
            gauges += [{**g, "stale": True} for g in last_ok.get(name, [])]
            continue
        fresh = fresh or []
        if fresh:
            last_ok[name] = fresh
        else:
            last_ok.pop(name, None)  # source deliberately absent (no login) - forget it
        gauges += fresh
    return gauges, errors


def collect(which: list[str] | None = None, max_age: float | None = None, parallel: bool = False) -> tuple[list[dict], list[str]]:
    """Fresh gauges plus the same stale fallback the menubar uses. Does not notify.

    The menubar calls this with no extra arguments, so it still fetches every source,
    in order. The CLI passes max_age to reuse a recent sample and parallel to fetch
    whatever is left at the same time.
    """
    names = list(SOURCE_FUNCS) if which is None else which
    try:
        last_ok = json.loads(LAST_OK_FILE.read_text()) if LAST_OK_FILE.exists() else {}
    except Exception:
        last_ok = {}
    if not isinstance(last_ok, dict):
        last_ok = {}
    gauges: list[dict] = []
    needed: list[str] = []
    for name in names:
        cached = last_ok.get(name) or []
        age = _sample_age(cached) if isinstance(cached, list) and cached else None
        if max_age is not None and isinstance(cached, list) and cached and age is not None and age <= max_age:
            gauges += cached
        else:
            needed.append(name)
    errors: list[str] = []
    if needed:
        fresh, errors = _fetch(needed, last_ok, parallel)
        gauges += fresh
        try:
            write_private(LAST_OK_FILE, last_ok)
        except Exception:
            pass
    return gauges, errors


def main() -> None:
    gauges, errors = collect()
    try:
        notify([g for g in gauges if not g["stale"]])
    except Exception as e:
        errors.append(f"notify: {e}")
    print(json.dumps({"gauges": gauges, "errors": errors}, ensure_ascii=False))


# --- CLI ----------------------------------------------------------------------
# Every window, grouped by account. The percent is how much of that limit is used.

MARKS = {"claude": "✳", "gemini": "✦", "codex": "⚛", "grok": "✕"}
NAME_COLOR = {"claude": "38;2;204;120;92", "gemini": "38;2;87;140;245", "codex": "38;2;115;184;161", "grok": "1"}
BAR_WIDTH = 20
_EIGHTH = "▏▎▍▌▋▊▉"


def window_name(g: dict) -> str:
    raw = g.get("label") or g.get("id") or ""
    left, sep, right = raw.partition(" · ")
    if not sep:
        return raw
    for src, dst in _WINDOW_WORDS:
        right = right.replace(src, dst)
    prefix = {"Gemini": "gemini ", "Other models": "other models "}.get(left, "")
    return f"{prefix}{right}".strip()


def _errors_by_source(errors: list[str]) -> dict[str, str]:
    out = {}
    for error in errors:
        name, sep, why = error.partition(": ")
        if sep and name in SOURCE_FUNCS:
            out[name] = why
    return out


def source_views(gauges: list[dict], errors: list[str], keys: list[str]) -> list[dict]:
    why = _errors_by_source(errors)
    views = []
    for key in keys:
        mine = [g for g in gauges if g.get("group") == key]
        views.append({
            "key": key, "name": SOURCE_NAME[key], "windows": mine,
            "error": why.get(key), "missing": not mine and key not in why,
        })
    return views


def _by_used(view: dict) -> tuple:
    """Least-used account first, judging by its fullest window. Stale accounts follow."""
    windows = view["windows"]
    if not windows:
        return (2, 0, view["name"])
    stale = all(g.get("stale") for g in windows)
    return (1 if stale else 0, max(int(g["percent"]) for g in windows), view["name"])


def _paint(code: str, text: str, tty: bool) -> str:
    if not tty:
        return text
    return f"\033[{code}m{text}\033[0m"


def _meter_code(percent: int) -> str:
    if percent >= 80:
        return "38;2;220;70;70"
    if percent >= 50:
        return "38;2;220;150;40"
    return "38;2;80;180;120"


def _bar(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    filled = percent / 100 * BAR_WIDTH
    whole = int(filled)
    eighth = int((filled - whole) * 8 + 1e-9)
    if eighth >= 8:
        whole += 1
        eighth = 0
    if whole >= BAR_WIDTH:
        return "█" * BAR_WIDTH
    parts = "█" * whole + (_EIGHTH[eighth - 1] if eighth else "")
    return parts + "░" * (BAR_WIDTH - len(parts))


def _blocks(views: list[dict], tty: bool, show_empty: bool) -> str:
    chosen = []
    for view in sorted(views, key=_by_used):
        if view["windows"] or view.get("error") or show_empty:
            chosen.append(view)
    if not chosen:
        return ""
    width = max([len(window_name(g)) for view in chosen for g in view["windows"]], default=0)
    blocks = []
    for view in chosen:
        title = _paint(NAME_COLOR.get(view["key"], "1"), f"{MARKS.get(view['key'], '•')} {view['name']}", tty)
        lines = [title]
        if not view["windows"]:
            lines.append(f"  {view.get('error') or 'not signed in'}")
        for g in view["windows"]:
            used = int(g["percent"])
            meter = _paint(_meter_code(used), f"{used:>3}%  {_bar(used)}", tty)
            stale = _paint("2", "  stale", tty) if g.get("stale") else ""
            lines.append(f"  {window_name(g):<{width}}  {meter}{stale}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def report_status(views: list[dict]) -> int:
    return 0 if any(v["windows"] for v in views) else 1


def format_report(views: list[dict], one: bool, tty: bool = False) -> str:
    return _blocks(views[:1] if one else views, tty, show_empty=one)


def running_as_cli(argv: list[str]) -> bool:
    """The menubar runs `python3 usage.py` and reads JSON. A name of usagebar, or any argument, is the CLI."""
    return Path(argv[0]).name in {"usagebar", "usagebar.py"} or len(argv) > 1


def cli(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="usagebar", description="Show how much of each AI subscription limit is used.")
    parser.add_argument("source", nargs="?", help="antigravity, grok, codex, or claude")
    args = parser.parse_args(argv[1:])
    if args.source is None:
        keys, one = list(SOURCE_FUNCS), False
    else:
        key = CLI_SOURCES.get(args.source.lower())
        if key is None:
            parser.error(f"unknown source {args.source!r}; choose antigravity, grok, codex, or claude")
        keys, one = [key], True
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    gauges, errors = collect(keys, max_age=CLI_MAX_AGE, parallel=True)
    views = source_views(gauges, errors, keys)
    sys.stdout.write(format_report(views, one, tty))
    return report_status(views)


if __name__ == "__main__":
    if running_as_cli(sys.argv):
        try:
            raise SystemExit(cli(sys.argv))
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except Exception:
                pass
            raise SystemExit(0)
    main()
