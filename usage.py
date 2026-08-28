#!/usr/bin/env python3
"""Usage collector for UsageBar. Prints one JSON object to stdout:

    {"gauges": [{"id", "short", "label", "percent", "reset", "group"}], "errors": [...]}

Sources (each is skipped silently when its credentials are not on this Mac):
  claude  - the endpoint Claude Code's /usage uses. OAuth token from the Keychain,
            sent only to api.anthropic.com. 5h session, weekly all-models, weekly per-model.
  gemini  - Antigravity's Cloud Code quota (shared by the `agy` CLI and the Antigravity
            IDE). Token from the Keychain item agy writes; refreshed with agy's own OAuth
            client (read from the agy binary, nothing stored in this repo).
  codex   - OpenAI Codex CLI rate limits from ~/.codex/auth.json.  UNVERIFIED: written
            from public descriptions of the endpoint, no Codex account was available.

Sends one macOS notification per limit per reset window when it crosses NOTIFY_AT.
Runs on the system /usr/bin/python3 (3.9) - keep it stdlib-only and 3.9-compatible.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("USAGEBAR_STATE", Path.home() / "Library/Application Support/UsageBar"))
NOTIFY_AT = int(os.environ.get("USAGEBAR_NOTIFY_AT", "80"))
NOTIFIED_FILE = STATE_DIR / "notified.json"
GEMINI_TOKEN_CACHE = STATE_DIR / "gemini_token.json"
GEMINI_USER_AGENT = "antigravity/1.1.19 darwin arm64"  # quota endpoint answers only to the Antigravity client
GEMINI_MODELS = {"gemini-3.7-flash-tiered": "Gemini 3.7 Flash", "gemini-3.1-pro-high": "Gemini 3.1 Pro"}
CODEX_AUTH = Path.home() / ".codex/auth.json"


# --- helpers -----------------------------------------------------------------

def parse_iso(value: str) -> datetime:
    """Python 3.9 fromisoformat rejects 'Z' and fractions that are not 3/6 digits."""
    value = value.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", value)
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")


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

def gemini_oauth_clients() -> list[tuple[str, str]]:
    """OAuth client id/secret pairs embedded in the agy binary (public desktop-app credentials)."""
    agy = shutil.which("agy")
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


def gemini_gauges() -> list[dict]:
    token = gemini_token()
    if not token:
        return []
    quota = http_json("https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                      {"Authorization": f"Bearer {token}", "User-Agent": GEMINI_USER_AGENT}, b"{}")
    by_id = {b["modelId"]: b for b in quota["buckets"]}
    return [gauge(f"gemini:{mid}", "G", label, (1 - by_id[mid].get("remainingFraction", 1)) * 100,
                  by_id[mid].get("resetTime"), "gemini")
            for mid, label in GEMINI_MODELS.items() if mid in by_id]


# --- Codex (UNVERIFIED) -------------------------------------------------------

def codex_gauges() -> list[dict]:
    """Codex CLI (ChatGPT login) rate limits. Endpoint/shape from public write-ups of
    CodexBar-style tools - adjust once someone with a Codex account can test it."""
    if not CODEX_AUTH.exists() or not shutil.which("codex"):  # stale auth.json without the CLI is common
        return []
    tokens = json.loads(CODEX_AUTH.read_text())["tokens"]
    data = http_json("https://chatgpt.com/backend-api/wham/usage",
                     {"Authorization": f"Bearer {tokens['access_token']}", "ChatGPT-Account-Id": tokens.get("account_id", "")})
    limits = data.get("rate_limits") or data
    out = []
    for key, label in (("primary_window", "Codex · 5 h"), ("secondary_window", "Codex · týždeň")):
        win = limits.get(key)
        if not win:
            continue
        reset = win.get("reset_at") or win.get("resets_at")
        if isinstance(reset, (int, float)):
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        out.append(gauge(f"codex:{key}", "X", label, win.get("used_percent", 0), reset, "codex"))
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
