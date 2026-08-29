# UsageBar

macOS menubar gauge for AI-coding subscription limits: **Claude Code** (5 h session,
weekly, weekly per-model), **Gemini / Antigravity** (`agy` CLI + IDE share one quota),
(5 h + weekly per model group — Gemini, and Claude/GPT used through Antigravity), and
**Codex** (5 h + weekly, once `codex login` has been run). No dependencies: one Python script (system
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
| Gemini / Antigravity | none when an `agy` / Antigravity language server is running (localhost RPC, no auth); otherwise Keychain item `gemini` / `antigravity` (written by `agy` login), refresh via the OAuth client embedded in the `agy` binary | local `RetrieveUserQuotaSummary`, else `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` — the same 5 h + weekly per-group numbers the IDE shows |
| Codex | `~/.codex/auth.json` (only when the `codex` CLI is installed and logged in via ChatGPT) | `chatgpt.com/backend-api/wham/usage` — same call CodexBar makes; shape verified against its source, not yet run against a live account |

Tokens are sent only to their own vendor's API. A refreshed Gemini access token is
cached in `~/Library/Application Support/UsageBar/` (mode 600). Nothing else leaves
the machine. All endpoints are internal/undocumented and may change without notice —
a broken source shows as a `⚠` row in the menu, the others keep working.

## Hooking up Codex

1. `npm i -g @openai/codex` (or `brew install codex`), then `codex login` — sign in with the
   ChatGPT account whose plan includes Codex. This writes `~/.codex/auth.json`.
2. Click **Obnoviť** in the menu (or wait 5 min). A `CODEX` section with the 5 h and weekly
   windows appears; `X ◔ 12%` joins the menubar.
3. Nothing to configure: API-key logins (`OPENAI_API_KEY`) have no rate-limit windows and are
   skipped on purpose.

## Before you publish or fork this

None of the endpoints here are documented or supported by their vendors. The tool
reads **your own** limits with **your own** login, read-only, and sends each token
only to that vendor's API. The Gemini source identifies itself as the installed
`agy` client (that is what the quota endpoint expects); if Google or Anthropic
change or close these endpoints the corresponding rows simply disappear from the
menu. Use it as a personal convenience, not as something to build a product on.

## Tweaks

| What | Where |
|---|---|
| Threshold for notifications | `NOTIFY_AT` in `usage.py` (or env `USAGEBAR_NOTIFY_AT`) |
| Antigravity group labels | `ANTIGRAVITY_GROUPS` in `usage.py` |
| Which gauges appear in the menubar | `headline` in `UsageBar.swift` |
| Refresh interval | `refreshSeconds` in `UsageBar.swift` |
| Colors | `color()` in `UsageBar.swift` (green < 50, orange < 80, red ≥ 80) |

Debug the collector alone: `/usr/bin/python3 usage.py | python3 -m json.tool`.
