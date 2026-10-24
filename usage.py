#!/usr/bin/env python3
"""Usage collector for UsageBar.

The menubar runs `python3 usage.py` with no arguments. That prints one JSON object:

    {"gauges": [{"id", "short", "label", "percent", "reset", "group"}], "errors": [...],
     "plans": {"claude": "Max 5×", "codex": "Plus"}}

Invoked as `usagebar`, it prints every window for every signed-in account.
`usagebar antigravity|grok|codex|claude` prints that account. A sample newer
than the menu-bar refresh is reused, so the command does not wait on the network
for numbers the menu already has. This path does not post the macOS notification.

Sources (each is skipped silently when its credentials are not on this Mac):
  claude  - Claude Code's built-in /usage command in a PTY, using its existing login.
            Account-wide 5h session, weekly all-models, weekly per-model.
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
import fcntl
import http.client
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import termios
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_DIR = Path(
    os.environ.get(
        "USAGEBAR_STATE", Path.home() / "Library/Application Support/UsageBar"
    )
)
NOTIFY_AT = int(os.environ.get("USAGEBAR_NOTIFY_AT", "80"))
NOTIFIED_FILE = STATE_DIR / "notified.json"
LAST_OK_FILE = STATE_DIR / "last_ok.json"
CLAUDE_RETRY_FILE = STATE_DIR / "claude_retry.json"
ANTIGRAVITY_CACHE = STATE_DIR / "antigravity_cache.json"
ANTIGRAVITY_CACHE_TTL = (
    120  # a manual "Obnoviť" right after the timer should not spawn agy again
)
ANTIGRAVITY_TIMEOUT = 40  # agy -p /usage takes 4-6 s warm, ~15 s cold
CODEX_AUTH = Path.home() / ".codex/auth.json"
GROK_AUTH = Path.home() / ".grok/auth.json"
GROK_VERSION = Path.home() / ".grok/version.json"
log = logging.getLogger("usagebar")
GROK_PROXY = os.environ.get(
    "GROK_CLI_CHAT_PROXY_BASE_URL", "https://cli-chat-proxy.grok.com/v1"
).rstrip("/")


# --- helpers -----------------------------------------------------------------


def parse_iso(value: str) -> datetime:
    """Python 3.9 fromisoformat rejects 'Z' and fractions that are not 3/6 digits."""
    value = value.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", value)
    if m is None:
        raise ValueError(f"not an ISO timestamp: {value!r}")
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3)}")


def find_binary(name: str) -> str | None:
    """launchd gives the app a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so PATH lookup
    alone misses Homebrew and ~/.local/bin installs - search the usual places explicitly."""
    found = shutil.which(name)
    if found:
        return found
    for directory in (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        Path.home() / ".local/bin",
        Path.home() / "bin",
        "/usr/sbin",
        "/sbin",
    ):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def http_json(url: str, headers: dict, body: bytes | None = None) -> dict:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def write_private(path: Path, payload: dict) -> None:
    """0600 from the first byte (chmod after write_text leaves a window with default perms)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gauge(
    id_: str, short: str, label: str, percent: float, reset: str | None, group: str
) -> dict:
    return {
        "id": id_,
        "short": short,
        "label": label,
        "percent": round(percent),
        "reset": reset,
        "group": group,
        "fetched_at": now_iso(),
        "stale": False,
    }


# --- Claude ------------------------------------------------------------------


def claude_state() -> dict:
    try:
        state = json.loads(CLAUDE_RETRY_FILE.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def claude_backoff() -> dict:
    state = claude_state()
    if state.get("source") != "cli":
        return {}  # an old direct-OAuth cooldown must not suppress the CLI source
    try:
        if parse_iso(state["retry_at"]) > datetime.now(timezone.utc):
            return state
    except (ValueError, TypeError, LookupError, AttributeError):
        pass
    return {}


def claude_retry_at(value: str | None) -> datetime:
    now = datetime.now(timezone.utc)
    try:
        seconds = int(value or "")
        if seconds >= 0:
            return now + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        pass
    try:
        retry = parsedate_to_datetime(value or "")
        if retry > now:
            return retry
    except (ValueError, TypeError, OverflowError):
        pass
    return now + timedelta(minutes=5)


def claude_text(raw: str) -> str:
    # Cursor-positioned words need spaces; merely removing ANSI joins labels together.
    raw = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", raw)
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[CG]", " ", raw)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).replace("\r", "\n")


def claude_reset(description: str) -> str | None:
    compact = re.sub(r"\s+", "", description)
    match = re.fullmatch(
        r"(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{1,2})(?:,|at)?)?"
        r"(\d{1,2})(?::(\d{2}))?(am|pm)(?:\(([^)]+)\))?",
        compact,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        zone = ZoneInfo(match[6]) if match[6] else datetime.now().astimezone().tzinfo
        now = datetime.now(zone)
        hour = int(match[3])
        minute = int(match[4] or 0)
        if not 1 <= hour <= 12 or not 0 <= minute < 60:
            return None
        hour = hour % 12 + (12 if match[5].lower() == "pm" else 0)
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if match[1]:
            month = [
                "jan",
                "feb",
                "mar",
                "apr",
                "may",
                "jun",
                "jul",
                "aug",
                "sep",
                "oct",
                "nov",
                "dec",
            ].index(match[1].lower()) + 1
            reset = reset.replace(month=month, day=int(match[2]))
            if reset < now:
                reset = reset.replace(year=reset.year + 1)
            if reset - now > timedelta(days=8):
                return None  # a past or unrecognized calendar reset must not become next year's
        elif reset <= now:
            reset += timedelta(days=1)
        return reset.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, ZoneInfoNotFoundError):
        return None


def claude_parse(raw: str) -> list[dict]:
    text = claude_text(raw)
    compact = re.sub(r"\s+", "", text).lower()
    if any(
        marker in compact
        for marker in ("ratelimit", "rate_limit", "toomanyrequests", "error:429")
    ):
        raise RuntimeError("Claude CLI usage rate limited; retry in 5 minutes")
    if "failedtoloadusagedata" in compact:
        raise RuntimeError(
            "Claude CLI could not load usage data; retry /usage in Claude"
        )
    if any(
        marker in compact
        for marker in (
            "notloggedin",
            "notauthenticated",
            "authentication_error",
        )
    ):
        raise RuntimeError("Claude CLI needs login; open Claude and run /login")
    labels = list(
        re.finditer(r"Current\s*(session|week)(?:\s*\(([^)]+)\))?", text, re.IGNORECASE)
    )
    out = {}
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        block = text[label.end() : end]
        block = re.split(r"What['’]s\s*contributing", block, flags=re.IGNORECASE)[0]
        percent = re.search(
            r"(\d+(?:\.\d+)?)\s*%\s*(used|left|remaining)\b", block, re.IGNORECASE
        )
        if not percent:
            continue
        used = float(percent[1])
        if not 0 <= used <= 100:
            raise ValueError("Claude CLI returned an invalid usage percentage")
        if percent[2].lower() != "used":
            used = 100 - used
        model = (label[2] or "").strip()
        if label[1].lower() == "session":
            kind, short, title = "session", "C", "5 h"
        elif not model or re.sub(r"\s+", "", model).lower() == "allmodels":
            kind, short, title = "weekly_all", "W", "týždeň"
        else:
            kind, short, title = f"weekly_scoped:{model}", model[:1], f"týždeň {model}"
        reset = re.search(r"Resets?\s*([^\n]+)", block, re.IGNORECASE)
        reset_at = claude_reset(reset[1]) if reset else None
        g = gauge(
            f"claude:{kind}", short, f"Claude · {title}", used, reset_at, "claude"
        )
        if reset:
            g["reset_description"] = reset[1].strip()
        out[g["id"]] = g
    if not out:
        raise ValueError("Claude CLI /usage returned no account quota percentages")
    return list(out.values())


def claude_screen(raw: str) -> str:
    """Replay Claude's bounded terminal viewport so trust decisions use the selected row."""
    rows, columns = 200, 160
    lines = [[" "] * columns for _ in range(rows)]
    row = column = 0
    saved = (0, 0)
    tokens = re.findall(
        r"\x1b(?:\[[0-?]*[ -/]*[@-~]?|\][\s\S]*?(?:\x07|\x1b\\|$)|.)|[^\x1b]+", raw
    )
    for token in tokens:
        if token.startswith("\x1b["):
            command, parameters = token[-1], token[2:-1]
            if command not in "ABCDGHfKJ" or re.search(r"[^0-9;]", parameters):
                continue
            values = [min(int(value or 0), rows) for value in parameters.split(";")[:2]]
            mode, amount = values[0], max(1, values[0])
            column = min(column, columns - 1)
            if command == "A":
                row = max(0, row - amount)
            elif command == "B":
                row = min(rows - 1, row + amount)
            elif command == "C":
                column = min(columns - 1, column + amount)
            elif command == "D":
                column = max(0, column - amount)
            elif command == "G":
                column = min(columns - 1, amount - 1)
            elif command in "Hf":
                row = min(rows - 1, amount - 1)
                column = min(
                    columns - 1, max(1, values[1] if len(values) > 1 else 1) - 1
                )
            elif mode in (0, 1, 2):
                cursor = row * columns + column
                start = row * columns if command == "K" else 0
                end = (row + 1) * columns if command == "K" else rows * columns
                for cell in range(
                    cursor if mode == 0 else start, cursor + 1 if mode == 1 else end
                ):
                    lines[cell // columns][cell % columns] = " "
        elif token == "\x1b7":
            saved = row, column
        elif token == "\x1b8":
            row, column = saved
        elif not token.startswith("\x1b"):
            for character in token:
                if character == "\r":
                    column = 0
                elif character == "\n":
                    row, column = row + 1, 0
                elif character == "\b":
                    column = max(0, column - 1)
                elif character == "\t":
                    column = min(columns - 1, (column // 8 + 1) * 8)
                elif ord(character) >= 32 and ord(character) != 127:
                    if unicodedata.combining(character):
                        if column:
                            lines[row][min(column - 1, columns - 1)] += character
                        continue
                    width = (
                        2
                        if unicodedata.east_asian_width(character) in ("W", "F")
                        or 0x1F300 <= ord(character) <= 0x1FAFF
                        else 1
                    )
                    if column + width > columns:
                        row, column = row + 1, 0
                    if row >= rows:
                        lines.pop(0)
                        lines.append([" "] * columns)
                        row = rows - 1
                    lines[row][column] = character
                    if width == 2:
                        lines[row][column + 1] = ""
                    column += width
                if row >= rows:
                    lines.pop(0)
                    lines.append([" "] * columns)
                    row = rows - 1
    return "\n".join("".join(line).rstrip() for line in lines).rstrip()


def claude_trust_key(screen: str) -> bytes | None:
    lines = screen.splitlines()
    for option_row, line in enumerate(lines):
        if "yes,itrustthisfolder" not in re.sub(r"\s+", "", line).lower():
            continue
        first = last = option_row
        while first > 0 and lines[first - 1].strip():
            first -= 1
        while last + 1 < len(lines) and lines[last + 1].strip():
            last += 1
        for marker_row in range(first, last + 1):
            if "❯" in lines[marker_row]:
                if marker_row == option_row:
                    return b"\r"
                return b"\x1b[B" if marker_row < option_row else b"\x1b[A"
    return None


def claude_capture(binary: str, timeout: float = 25) -> str:
    # Match CodexBar's CLI source: built-in /usage owns auth; UsageBar never reads login secrets.
    directory = STATE_DIR / "ClaudeProbe"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("Claude probe directory must not be a symlink")
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")
    }
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
        "CLAUDECODE",
    ):
        environment.pop(key, None)
    environment.update(
        TERM="xterm-256color", DISABLE_AUTOUPDATER="1", PWD=str(directory)
    )
    arguments = [
        binary,
        "--allowed-tools",
        "",
        "--strict-mcp-config",
        "--settings",
        '{"disableAllHooks":true,"remoteControlAtStartup":false}',
    ]
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 200, 160, 0, 0))
        # Acquire /dev/tty in a fresh interpreter, avoiding threaded preexec_fn callbacks.
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])",
                *arguments,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=directory,
            env=environment,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        ready_at = time.monotonic() + 2
        deadline = ready_at + timeout
        output = bytearray()
        query_tail = b""
        sent = dirty = trust_pending = False
        retried = False
        sent_at = None
        trust_keys = 4
        confirmations = set()
        last_output = time.monotonic()
        settled = None
        screen = ""
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.06)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 2_000_000:
                    raise RuntimeError("Claude CLI usage output exceeded 2 MB")
                query = query_tail + chunk
                if b"\x1b[6n" in query:
                    os.write(master, b"\x1b[1;1R")
                query_tail = query[-3:]
                last_output = time.monotonic()
                dirty = True
            now = time.monotonic()
            if dirty and (now - last_output >= 0.2 or process.poll() is not None):
                try:
                    screen = claude_screen(output.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
                dirty = False
                compact = re.sub(r"\s+", "", screen).lower()
                if any(
                    word in compact
                    for word in (
                        "password",
                        "keychain",
                        "selectloginmethod",
                        "let'sgetyousignedin",
                        "pastethecode",
                                    "notloggedin",
                    )
                ):
                    raise RuntimeError(
                        "Claude CLI needs interactive login; open Claude and sign in"
                    )
                trust_key = claude_trust_key(screen)
                trust_pending = trust_key is not None or any(
                    marker in compact
                    for marker in (
                        "quicksafetycheck:",
                        "doyoutrustthefilesinthisfolder?",
                        "readytocodehere?",
                    )
                )
                if trust_pending:
                    if trust_key is not None and trust_keys:
                        os.write(master, trust_key)
                        trust_keys -= 1
                        ready_at = now + 2
                else:
                    if sent:
                        if "failedtoloadusagedata" in compact:
                            return screen
                        try:
                            claude_parse(screen)
                            if settled is None:
                                settled = now + 2
                        except ValueError:
                            pass
                        except RuntimeError:
                            return screen
                    markers = ("pressentertocontinue",)
                    if sent:
                        markers += ("showplan", "showsessioncost,planusage")
                    for marker in markers:
                        if marker in compact and marker not in confirmations:
                            os.write(master, b"\r")
                            confirmations.add(marker)
                            if not sent:
                                ready_at = now + 2
            if not sent and not trust_pending and not dirty and now >= ready_at:
                os.write(master, b"/usage\r")
                sent = True
                sent_at = now
                output.clear()
                screen = ""
                deadline = now + timeout
            elif (
                sent
                and not retried
                and not dirty
                and sent_at is not None
                and now - sent_at >= 2
            ):
                compact = re.sub(r"\s+", "", screen).lower()
                # Claude can paint startup before its input handler is ready. CodexBar
                # retries /usage once when the first capture is still the startup screen.
                if (
                    "claudecodev" in compact
                    and re.search(r"(?m)^\s*❯\s*$", screen)
                    and not any(
                        marker in compact
                        for marker in (
                            "currentsession",
                            "currentweek",
                            "loadingusage",
                            "failedtoloadusagedata",
                            "settingsstatusconfig",
                        )
                    )
                ):
                    os.write(master, b"/usage\r")
                    retried = True
                    output.clear()
                    screen = ""
                    deadline = now + timeout
            if settled is not None and now >= settled:
                return claude_screen(output.decode("utf-8", errors="replace"))
            if process.poll() is not None:
                break
        if sent and output:
            return claude_screen(output.decode("utf-8", errors="replace"))
        raise RuntimeError(
            "Claude CLI usage probe timed out; open Claude and try /usage"
        )
    finally:
        if slave >= 0:
            os.close(slave)
        if process is not None and process.poll() is None:
            try:
                os.write(master, b"/exit\r")
            except OSError:
                pass
        os.close(master)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            # The probe owns this group. Reap children even if the CLI root exited first.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:
                    process.kill()
            process.wait(timeout=5)


def claude_gauges() -> list[dict]:
    backoff = claude_backoff()
    if backoff:
        raise RuntimeError(f"Rate limited; retry after {backoff['retry_at']}")
    binary = find_binary("claude")
    if not binary:
        return []
    text = claude_capture(binary)
    try:
        gauges = claude_parse(text)
    except RuntimeError as exc:
        if "rate limited" in str(exc):
            try:
                write_private(
                    CLAUDE_RETRY_FILE,
                    {
                        **claude_state(),
                        "source": "cli",
                        "retry_at": claude_retry_at(None).isoformat(timespec="seconds"),
                    },
                )
            except OSError:
                pass
        raise
    plan = re.search(
        r"Claude\s+(Max|Pro|Team|Enterprise)\b", claude_text(text), re.IGNORECASE
    )
    try:
        write_private(
            CLAUDE_RETRY_FILE,
            {"source": "cli", "plan": plan[1].capitalize() if plan else None},
        )
    except OSError:
        pass
    return gauges


def claude_plan() -> str | None:
    return claude_state().get("plan")


# --- Gemini / Antigravity ----------------------------------------------------
# Same data the Antigravity IDE shows: per model-group 5h + weekly windows.
# 1. A running agy / Antigravity language server on localhost, if ANTIGRAVITY_CSRF_TOKEN
#    is set (agy 1.2+ rejects RPCs without the x-codeium-csrf-token header). Off by default.
# 2. `agy -p /usage --output-format json` (agy 1.1.11+). Authoritative, 4-15 s.
# The Cloud Code endpoint (cloudcode-pa.googleapis.com retrieveUserQuotaSummary) is NOT
# used any more: it answers with remainingFraction 1 for every bucket, i.e. a fake 0 %.

ANTIGRAVITY_RPC = (
    "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
)
ANTIGRAVITY_GROUPS = {
    "Gemini Models": "Gemini",
    "Claude and GPT models": "Other models",
}
ANTIGRAVITY_WINDOWS = {"5h": "5 h", "weekly": "týždeň"}


def antigravity_local_ports() -> list[int]:
    lsof = find_binary("lsof")
    if not lsof:
        return []
    r = subprocess.run(
        [lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fcn"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    ports, cmd = [], ""
    for line in r.stdout.splitlines():
        if line.startswith("c"):
            cmd = line[1:].lower()
        elif line.startswith("n127.0.0.1:") and (
            "agy" in cmd or "language_server" in cmd or "antigravity" in cmd
        ):
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
                req = urllib.request.Request(
                    f"{scheme}://127.0.0.1:{port}{ANTIGRAVITY_RPC}",
                    data=b"{}",
                    headers=headers,
                )
                with urllib.request.urlopen(
                    req, timeout=2, context=ssl._create_unverified_context()
                ) as r:
                    data = json.load(r)
            except (OSError, http.client.HTTPException, ValueError):
                continue  # not this port/scheme; the CLI path below is authoritative anyway
            if isinstance(data, dict) and "response" in data:
                return data["response"]
    return None


def antigravity_cli_summary() -> dict:
    now = datetime.now(timezone.utc).timestamp()
    if ANTIGRAVITY_CACHE.exists():
        try:
            cached = json.loads(ANTIGRAVITY_CACHE.read_text())
        except (OSError, ValueError):
            cached = None  # an unreadable cache only means asking agy again
        stamp = cached.get("timestamp") if isinstance(cached, dict) else None
        if (
            isinstance(stamp, (int, float))
            and now - stamp < ANTIGRAVITY_CACHE_TTL
            and "data" in cached
        ):
            return cached["data"]
    agy = find_binary("agy")
    if not agy:
        raise RuntimeError("agy nie je nainštalované")
    # agy mistakes a failed token refresh while offline for a logged-out account and opens a
    # Google login page in the browser - on every poll. Don't start it unless Google is reachable.
    try:
        socket.create_connection(("oauth2.googleapis.com", 443), timeout=3).close()
    except OSError:
        raise RuntimeError("bez pripojenia k Google, agy sa nespúšťa")
    try:
        r = subprocess.run(
            [agy, "-p", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=ANTIGRAVITY_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"agy neodpovedalo do {ANTIGRAVITY_TIMEOUT} s")
    if r.returncode != 0:
        raise RuntimeError(
            f"agy skončilo s kódom {r.returncode}: {(r.stderr or r.stdout).strip()[:60]}"
        )
    summary = json.loads(r.stdout).get("command", {}).get("data")
    antigravity_buckets(
        summary
    )  # validate before caching, so a broken shape is never served from cache
    try:
        write_private(
            ANTIGRAVITY_CACHE,
            {"timestamp": datetime.now(timezone.utc).timestamp(), "data": summary},
        )
    except OSError:
        pass  # cache is an optimisation, never a reason to lose a good sample
    return summary


def antigravity_buckets(
    summary: dict | None,
) -> list[tuple[str, str, str, float, str | None]]:
    """Keep provider quota groups distinct as new models gain independent limits.

    Invalid buckets still fail the source instead of inventing a zero usage window.
    """
    groups = (summary or {}).get("groups")
    if not groups:
        raise ValueError("odpoveď bez groups")
    out = []
    for group in groups:
        provider_name = group.get("displayName") or group.get("name")
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ValueError("skupina bez názvu")
        name = ANTIGRAVITY_GROUPS.get(provider_name, provider_name)
        buckets = group.get("buckets")
        if not buckets:
            raise ValueError(f"skupina {name} bez buckets")
        for b in buckets:
            bid = b.get("bucketId") or b.get("id")
            rem = b.get("remainingFraction", b.get("remaining_fraction"))
            window = b.get("window")
            if (
                not bid
                or not isinstance(rem, (int, float))
                or not 0 <= rem <= 1
                or window not in ANTIGRAVITY_WINDOWS
            ):
                raise ValueError(
                    f"neplatný bucket {bid!r}: rem={rem!r} window={window!r}"
                )
            out.append(
                (
                    name,
                    bid,
                    window,
                    float(rem),
                    b.get("resetTime") or b.get("reset_time"),
                )
            )
    return out


def gemini_gauges() -> list[dict]:
    summary = antigravity_local_summary() or antigravity_cli_summary()
    out = []
    rows = sorted(
        antigravity_buckets(summary), key=lambda t: (t[0] != "Gemini", t[2] != "5h")
    )  # Gemini group first, 5h first
    for name, bid, window, rem, reset in rows:
        out.append(
            gauge(
                f"antigravity:{bid}",
                "G" if name == "Gemini" else "A",
                f"{name} · {ANTIGRAVITY_WINDOWS[window]}",
                (1 - rem) * 100,
                reset,
                "gemini",
            )
        )
    return out


# --- Codex ---------------------------------------------------------------------
# Endpoint and response shape taken from CodexBar's CodexOAuthUsageFetcher.swift
# (GET backend-api/wham/usage -> rate_limit.primary_window/secondary_window with
# used_percent, reset_at epoch, limit_window_seconds). Not exercised against a live
# Codex account yet - if it breaks, the row shows as a warning in the menu.


def codex_gauges() -> list[dict]:
    if not CODEX_AUTH.exists() or not find_binary(
        "codex"
    ):  # stale auth.json without the CLI is common
        return []
    tokens = json.loads(CODEX_AUTH.read_text()).get("tokens") or {}
    if not tokens.get("access_token"):
        return []
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "User-Agent": "UsageBar",
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage", headers=headers
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    rate = data.get("rate_limit")
    if not isinstance(rate, dict):
        raise TypeError("odpoveď bez rate_limit (zmenilo sa API?)")
    out = []
    for key in ("primary_window", "secondary_window"):
        win = rate.get(key)
        if not win:
            continue
        used = win.get("used_percent")
        if not isinstance(used, (int, float)):
            raise TypeError(f"{key} bez used_percent")
        seconds = win.get("limit_window_seconds") or 0
        window = (
            f"{round(seconds / 3600)} h"
            if seconds and seconds < 48 * 3600
            else "týždeň"
        )
        reset = win.get("reset_at")
        if isinstance(reset, (int, float)):
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        out.append(
            gauge(f"codex:{key}", "X", f"Codex · {window}", used, reset, "codex")
        )
    if not out:
        raise RuntimeError("rate_limit bez okien (účet bez limitov?)")
    return out


def codex_plan() -> str | None:
    """chatgpt_plan_type claim of the id_token in ~/.codex/auth.json (plus, pro, team, ...). Decoded locally, never sent."""
    if not CODEX_AUTH.exists():
        return None
    token = (json.loads(CODEX_AUTH.read_text()).get("tokens") or {}).get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    import base64

    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
    return str(plan).capitalize() if plan else None


# Only vendors whose plan is readable locally with certainty. Antigravity exposes no tier
# through agy; the Grok billing payload is undocumented and untested here.
PLAN_FUNCS = {"claude": claude_plan, "codex": codex_plan}


def collect_plans() -> dict[str, str]:
    """Subscription name per vendor, e.g. {"claude": "Max 5×", "codex": "Plus"}. A failure just leaves the vendor out."""
    out = {}
    for name, fn in PLAN_FUNCS.items():
        try:
            plan = fn()
        except SOURCE_ERRORS:
            plan = None
        if plan:
            out[name] = plan
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
        data = json.loads(GROK_VERSION.read_text())
    except (OSError, ValueError):
        data = None  # fall back to asking the CLI itself
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
        return version
    grok = find_binary("grok")
    if not grok:
        return "0.0.0"
    try:
        r = subprocess.run(
            [grok, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return "0.0.0"
    m = re.search(r"(\d+\.\d+\.\d+)", r.stdout or "")
    return m.group(1) if m else "0.0.0"


def grok_load_auth() -> dict:
    try:
        data = json.loads(GROK_AUTH.read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"auth.json sa nedá čítať ({type(e).__name__})")
    if not isinstance(data, dict):
        raise TypeError("auth.json nemá očakávaný tvar")
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
    except (ValueError, TypeError):  # TypeError: an offset-naive timestamp
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
        r = subprocess.run(
            [grok, "models"],
            capture_output=True,
            text=True,
            timeout=40,
            stdin=subprocess.DEVNULL,
            check=False,
        )
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
    except ValueError:
        return None
    return value


def grok_credit_percent(config: dict) -> float | None:
    """None when this payload has no credit window. 0 when the CLI omits a weekly percent."""
    if "creditUsagePercent" in config and config["creditUsagePercent"] is not None:
        raw = config["creditUsagePercent"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError("creditUsagePercent nie je číslo")
        return grok_percent(float(raw), "creditUsagePercent")
    products = config.get("productUsage")
    if products is not None and not isinstance(products, list):
        raise TypeError("productUsage nie je zoznam")
    for item in products or []:
        if not isinstance(item, dict):
            raise TypeError("productUsage má neznámy tvar")
        name = str(item.get("product") or "").lower().replace("-", "").replace("_", "")
        if name not in ("grokbuild", "build"):
            continue
        raw = item.get("usagePercent")
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError("productUsage.usagePercent nie je číslo")
        return grok_percent(float(raw), "productUsage")
    period = config.get("currentPeriod")
    if isinstance(period, dict) and "WEEK" in str(period.get("type") or "").upper():
        return 0.0  # the CLI drops the field entirely while the week is still at 0 %
    return None


def grok_config(payload: dict) -> dict:
    config = payload.get("config")
    if isinstance(config, dict):
        return config
    if any(
        k in payload
        for k in ("creditUsagePercent", "monthlyLimit", "currentPeriod", "productUsage")
    ):
        return payload
    raise RuntimeError("odpoveď bez config (zmenilo sa API?)")


def grok_fetch(token: str, query: str) -> dict:
    version = grok_client_version()
    req = urllib.request.Request(
        f"{GROK_PROXY}/billing{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": version,
            "x-grok-client-identifier": "grok-shell",
            "User-Agent": f"grok-cli/{version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GrokAuthExpired(f"billing HTTP {e.code}")
        raise RuntimeError(f"billing HTTP {e.code}")
    if not isinstance(payload, dict):
        raise TypeError("billing nevrátilo objekt")
    return grok_config(payload)


def grok_windows(
    weekly: dict, monthly: dict | None
) -> list[tuple[str, str, str, float, str | None]]:
    """(id, short, label, percent, reset). Raises on a shape that would otherwise draw a fake 0 %."""
    out = []
    period = (
        weekly.get("currentPeriod")
        if isinstance(weekly.get("currentPeriod"), dict)
        else {}
    )
    kind = str(period.get("type") or "")
    credits_monthly = "MONTH" in kind.upper() and "WEEK" not in kind.upper()
    credit = grok_credit_percent(weekly)
    if credit is not None:
        label = "Grok · mesiac" if credits_monthly else "Grok · týždeň"
        out.append(
            (
                "grok:weekly",
                "K",
                label,
                credit,
                grok_reset(period.get("end"))
                or grok_reset(weekly.get("billingPeriodEnd")),
            )
        )
    if not credits_monthly and monthly:
        limit = grok_optional_money(monthly, "monthlyLimit")
        used = grok_optional_money(monthly, "used")
        if limit is not None and limit > 0 and used is not None:
            out.append(
                (
                    "grok:monthly",
                    "K",
                    "Grok · mesiac",
                    grok_percent(used / limit * 100, "monthly"),
                    grok_reset(monthly.get("billingPeriodEnd")),
                )
            )
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
        reset = grok_reset(reset_source.get("billingPeriodEnd")) or grok_reset(
            period.get("end")
        )
        out.append(
            (
                "grok:ondemand",
                "K",
                "Grok · pay-as-you-go",
                grok_percent(spent / cap * 100, "onDemand"),
                reset,
            )
        )
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
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{msg}" with title "UsageBar" sound name "Glass"',
                ],
                timeout=10,
                check=False,
            )
            seen[key] = True
    NOTIFIED_FILE.write_text(
        json.dumps({k: v for k, v in seen.items() if k.split("@", 1)[1] > now_key})
    )


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

# What a source (or the notifier) fails with in practice: network, TLS and subprocess
# trouble, JSON that does not parse or does not have the shape we read, and the
# Runtime/Value/TypeErrors the parsers raise on purpose. These become an error row and
# the stale fallback. Anything else is a bug; it still must not take the other sources
# down with it, so it gets the same treatment plus a traceback on stderr.
SOURCE_ERRORS = (
    OSError,  # includes urllib.error.URLError/HTTPError, socket timeouts, ssl.SSLError
    http.client.HTTPException,
    subprocess.SubprocessError,
    ValueError,  # includes json.JSONDecodeError, UnicodeDecodeError
    TypeError,
    LookupError,
    AttributeError,
    ArithmeticError,
    RuntimeError,
)


def _source_error(exc: Exception) -> str:
    return (
        str(exc)[:80]
        if isinstance(exc, (RuntimeError, ValueError, TypeError))
        else f"{type(exc).__name__}: {str(exc)[:70]}"
    )


def _sample_age(gauges: list[dict]) -> float | None:
    now = datetime.now(timezone.utc)
    ages = []
    for g in gauges:
        reset = g.get("reset")
        if reset:
            try:
                if parse_iso(reset) <= now:
                    return None  # this sample belongs to the previous window
            except (ValueError, TypeError):
                return None
        raw = g.get("fetched_at")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            ages.append((now - parse_iso(raw)).total_seconds())
        except (ValueError, TypeError):  # TypeError: an offset-naive timestamp
            return None
    return max(ages) if ages else None


def _fetch_one(name: str) -> tuple[str, list[dict] | None, str | None]:
    try:
        return name, SOURCE_FUNCS[name](), None
    except SOURCE_ERRORS as exc:
        return name, None, _source_error(exc)
    except Exception as exc:
        log.exception("source %s failed unexpectedly", name)
        return name, None, _source_error(exc)


def _fetch(
    names: list[str], last_ok: dict, parallel: bool
) -> tuple[list[dict], list[str]]:
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


def collect(
    which: list[str] | None = None, max_age: float | None = None, parallel: bool = False
) -> tuple[list[dict], list[str]]:
    """Fresh gauges plus the same stale fallback the menubar uses. Does not notify.

    The menubar calls this with no extra arguments, so it still fetches every source,
    in order. The CLI passes max_age to reuse a recent sample and parallel to fetch
    whatever is left at the same time.
    """
    names = list(SOURCE_FUNCS) if which is None else which
    try:
        last_ok = json.loads(LAST_OK_FILE.read_text()) if LAST_OK_FILE.exists() else {}
    except (OSError, ValueError):
        last_ok = {}
    if not isinstance(last_ok, dict):
        last_ok = {}
    gauges: list[dict] = []
    needed: list[str] = []
    for name in names:
        cached = last_ok.get(name) or []
        age = _sample_age(cached) if isinstance(cached, list) and cached else None
        if (
            max_age is not None
            and isinstance(cached, list)
            and cached
            and age is not None
            and age <= max_age
        ):
            gauges += cached
        else:
            needed.append(name)
    errors: list[str] = []
    if needed:
        fresh, errors = _fetch(needed, last_ok, parallel)
        gauges += fresh
        try:
            write_private(LAST_OK_FILE, last_ok)
        except OSError:
            pass  # the gauges are already in hand; only the next stale fallback is lost
    return gauges, errors


def main() -> None:
    gauges, errors = collect()
    try:
        notify([g for g in gauges if not g["stale"]])
    except SOURCE_ERRORS as e:
        errors.append(f"notify: {e}")
    except Exception as e:
        log.exception("notify failed unexpectedly")
        errors.append(f"notify: {e}")
    print(
        json.dumps(
            {"gauges": gauges, "errors": errors, "plans": collect_plans()},
            ensure_ascii=False,
        )
    )


# --- CLI ----------------------------------------------------------------------
# Every window, grouped by account. The percent is how much of that limit is used.

MARKS = {"claude": "✳", "gemini": "✦", "codex": "⚛", "grok": "✕"}
NAME_COLOR = {
    "claude": "38;2;204;120;92",
    "gemini": "38;2;87;140;245",
    "codex": "38;2;115;184;161",
    "grok": "1",
}
BAR_WIDTH = 20
_EIGHTH = "▏▎▍▌▋▊▉"


def window_name(g: dict) -> str:
    raw = g.get("label") or g.get("id") or ""
    left, sep, right = raw.partition(" · ")
    if not sep:
        return raw
    for src, dst in _WINDOW_WORDS:
        right = right.replace(src, dst)
    prefix = {"Gemini": "gemini ", "Other models": "other models "}.get(
        left, f"{left} " if g.get("group") == "gemini" else ""
    )
    return f"{prefix}{right}".strip()


def _errors_by_source(errors: list[str]) -> dict[str, str]:
    out = {}
    for error in errors:
        name, sep, why = error.partition(": ")
        if sep and name in SOURCE_FUNCS:
            out[name] = why
    return out


def source_views(
    gauges: list[dict], errors: list[str], keys: list[str], plans: dict | None = None
) -> list[dict]:
    why = _errors_by_source(errors)
    views = []
    for key in keys:
        mine = [g for g in gauges if g.get("group") == key]
        views.append(
            {
                "key": key,
                "name": SOURCE_NAME[key],
                "windows": mine,
                "plan": (plans or {}).get(key),
                "error": why.get(key),
                "missing": not mine and key not in why,
            }
        )
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
    width = max(
        [len(window_name(g)) for view in chosen for g in view["windows"]], default=0
    )
    blocks = []
    for view in chosen:
        title = _paint(
            NAME_COLOR.get(view["key"], "1"),
            f"{MARKS.get(view['key'], '•')} {view['name']}",
            tty,
        )
        if view.get("plan"):
            title += _paint("2", f"  {view['plan']}", tty)
        lines = [title]
        if not view["windows"]:
            lines.append(f"  {view.get('error') or 'not signed in'}")
        for g in view["windows"]:
            used = int(g["percent"])
            meter = _paint(_meter_code(used), f"{used:>3}%  {_bar(used)}", tty)
            stale = _paint("2", "  stale", tty) if g.get("stale") else ""
            lines.append(f"  {window_name(g):<{width}}  {meter}{stale}")
        if view["windows"] and view.get("error"):
            lines.append(f"  {view['error']}")
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
    parser = argparse.ArgumentParser(
        prog="usagebar",
        description="Show how much of each AI subscription limit is used.",
    )
    parser.add_argument("source", nargs="?", help="antigravity, grok, codex, or claude")
    args = parser.parse_args(argv[1:])
    if args.source is None:
        keys, one = list(SOURCE_FUNCS), False
    else:
        key = CLI_SOURCES.get(args.source.lower())
        if key is None:
            parser.error(
                f"unknown source {args.source!r}; choose antigravity, grok, codex, or claude"
            )
        keys, one = [key], True
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    gauges, errors = collect(keys, max_age=CLI_MAX_AGE, parallel=True)
    views = source_views(gauges, errors, keys, collect_plans())
    sys.stdout.write(format_report(views, one, tty))
    return report_status(views)


if __name__ == "__main__":
    if running_as_cli(sys.argv):
        try:
            raise SystemExit(cli(sys.argv))
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except OSError:
                pass  # the reader is gone; the unflushed rest has nowhere to go
            raise SystemExit(0)
    main()
