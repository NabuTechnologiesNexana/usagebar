# UsageBar

macOS menubar gauge for AI-coding subscription limits: **Claude Code** (5 h session,
weekly, weekly per-model), **Gemini / Antigravity** (`agy` CLI + IDE share one quota),
(5 h + weekly per model group — Gemini, and Claude/GPT used through Antigravity), and
**Codex** (5 h + weekly, once `codex login` has been run). No dependencies: one Python script (system
`/usr/bin/python3`) + one Swift file compiled with `swiftc`.

```
✳ 59% 7d   ✦ 22% 7d   ⚛ 6%     ← menubar: vendor mark + the fullest window per vendor ("7d" = the weekly one)
```

Click for **Kam prepnúť teraz** (vendors ranked by headroom, with the limiting window and its
reset) and all gauges grouped by vendor, with progress bars and reset countdowns. When a source
fails, its last good numbers stay in the menu greyed out with "zastarané · pred N min" and the
reason as a ⚠ row; nothing is invented. A macOS notification fires once per limit per reset
window when it crosses 80 %.

Vendor marks are the logo shapes from [Simple Icons](https://simpleicons.org) in `icons/`
(CC0 collection; the marks remain their owners' trademarks), tinted per vendor. Without the
files the app falls back to in-house vector renditions drawn in `UsageBar.swift`.

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
| Gemini / Antigravity | the `agy` login itself: `agy -p /usage --output-format json` (agy 1.1.11+), cached 120 s | agy's own quota command — the same 5 h + weekly per-group numbers the IDE shows. A running language server is asked first only when `ANTIGRAVITY_CSRF_TOKEN` is set (agy 1.2+ rejects the localhost RPC without it). The Cloud Code endpoint is deliberately not used: it answers "100 % remaining" for everything |
| Codex | `~/.codex/auth.json` (only when the `codex` CLI is installed and logged in via ChatGPT) | `chatgpt.com/backend-api/wham/usage` — same call CodexBar makes |

Tokens are sent only to their own vendor's API. `~/Library/Application Support/UsageBar/`
(mode 600) holds the agy quota cache and the last good gauges per source. Nothing else leaves
the machine. All endpoints are internal/undocumented and may change without notice — an
unexpected response shape is reported as a `⚠` row (never rendered as 0 %), the last good
numbers stay greyed out, the other sources keep working.

## Hooking up Codex

1. `npm i -g @openai/codex` (or `brew install codex`), then `codex login` — sign in with the
   ChatGPT account whose plan includes Codex. This writes `~/.codex/auth.json`.
2. Click **Obnoviť** in the menu (or wait 5 min). A `CODEX` section with the 5 h and weekly
   windows appears; `X ◔ 12%` joins the menubar.
3. Nothing to configure: API-key logins (`OPENAI_API_KEY`) have no rate-limit windows and are
   skipped on purpose.

On a Plus plan, pin a cheaper default in `~/.codex/config.toml` (`model = "gpt-5.6-terra"`) —
with an empty config Codex runs the flagship `gpt-5.6-sol` on everything and the 5 h window
drains fast.

## Notes for other machines

Binaries are resolved by `find_binary()`, not by `PATH`: launchd starts the app with a minimal
`PATH`, so a Homebrew `codex` or a `~/.local/bin/agy` is invisible to `shutil.which` and the
matching section silently disappears from the menu. Add any unusual install dir there.

## Before you publish or fork this

None of the endpoints here are documented or supported by their vendors. The tool
reads **your own** limits with **your own** login, read-only, and sends each token
only to that vendor's API. If Google, OpenAI or Anthropic change or close these endpoints the corresponding
section turns grey and shows why. Use it as a personal convenience, not as something to build a product on.

## Tweaks

| What | Where |
|---|---|
| Threshold for notifications | `NOTIFY_AT` in `usage.py` (or env `USAGEBAR_NOTIFY_AT`) |
| Antigravity group labels | `ANTIGRAVITY_GROUPS` in `usage.py` |
| Which gauge represents a vendor in the menubar | `limiting()` in `UsageBar.swift` (the fullest window; for Antigravity only the Gemini group) |
| Vendor marks and colours | `icons/*.svg`, `vendorGlyph()` / `vendorColor()` in `UsageBar.swift` |
| agy timeout / cache | `ANTIGRAVITY_TIMEOUT`, `ANTIGRAVITY_CACHE_TTL` in `usage.py` |
| Refresh interval | `refreshSeconds` in `UsageBar.swift` |
| Colors | `color()` in `UsageBar.swift` (green < 50, orange < 80, red ≥ 80) |

Debug the collector alone: `/usr/bin/python3 usage.py | python3 -m json.tool`.
