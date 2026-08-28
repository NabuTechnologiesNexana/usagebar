# UsageBar

macOS menubar gauge for AI-coding subscription limits: **Claude Code** (5 h session,
weekly, weekly per-model), **Gemini / Antigravity** (`agy` CLI + IDE share one quota),
and **Codex** (prepared, unverified). No dependencies: one Python script (system
`/usr/bin/python3`) + one Swift file compiled with `swiftc`.

```
C ◔ 3%   F ◔ 32%   G ◔ 0%        ← menubar: letter + ring per headline gauge
```

Click for all gauges with progress bars and reset countdowns. A macOS notification
fires once per limit per reset window when it crosses 80 %.

## Install

Requires Xcode Command Line Tools (`xcode-select --install`).

```sh
git clone <this repo> ~/usagebar
~/usagebar/install.sh        # builds, registers a launchd agent, starts it
```

Update: `git pull && ./install.sh`. Remove: `./uninstall.sh`.

## Where the numbers come from

| Source | Credentials (read only, never stored by this tool) | Endpoint |
|---|---|---|
| Claude | Keychain item `Claude Code-credentials` (written by Claude Code login) | `api.anthropic.com/api/oauth/usage` — same as `/usage` in Claude Code |
| Gemini / Antigravity | Keychain item `gemini` / `antigravity` (written by `agy` login); refresh uses the OAuth client embedded in the `agy` binary | `daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` |
| Codex | `~/.codex/auth.json` (only when the `codex` CLI is installed) | `chatgpt.com/backend-api/wham/usage` — **untested**, adjust `codex_gauges()` |

Tokens are sent only to their own vendor's API. A refreshed Gemini access token is
cached in `~/Library/Application Support/UsageBar/` (mode 600). Nothing else leaves
the machine. All endpoints are internal/undocumented and may change without notice —
a broken source shows as a `⚠` row in the menu, the others keep working.

## Tweaks

| What | Where |
|---|---|
| Threshold for notifications | `NOTIFY_AT` in `usage.py` (or env `USAGEBAR_NOTIFY_AT`) |
| Which Gemini models to show | `GEMINI_MODELS` in `usage.py` (see all bucket ids: run the endpoint, or `agy models`) |
| Which gauges appear in the menubar | `headline` in `UsageBar.swift` |
| Refresh interval | `refreshSeconds` in `UsageBar.swift` |
| Colors | `color()` in `UsageBar.swift` (green < 50, orange < 80, red ≥ 80) |

Debug the collector alone: `/usr/bin/python3 usage.py | python3 -m json.tool`.
