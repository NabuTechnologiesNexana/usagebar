# UsageBar

macOS menubar gauge for AI-coding subscription limits: **Claude Code** (5 h session,
weekly, weekly per-model), **Gemini / Antigravity** (`agy` CLI + IDE share one quota),
(5 h + weekly per model group — Gemini, and Claude/GPT used through Antigravity),
**Codex** (5 h + weekly, once `codex login` has been run), and **Grok** (weekly credit
window, plus the monthly allowance when the plan has one, once `grok login` has been run).
No dependencies: one Python script (system `/usr/bin/python3`) + one Swift file compiled with `swiftc`.

```
✳ 29%   ✦ 0%   ⚛ 6%   ✕ 12%   ← menubar: vendor mark + current window per vendor
```

While a Claude Code turn, a `codex exec` or an `agy -p` run is in progress, that vendor's slot
alternates every 3 s between its percent and the elapsed time of the run (orange).

Click for **Beží teraz** (runs in progress: project, model, tokens written so far, elapsed),
**Kam prepnúť teraz** (vendors ranked by headroom, with the limiting window and its reset) and
all gauges grouped by vendor, with the subscription name in the header (Max 5×, Plus), progress
bars and, for every window, how long it has been open and when it resets ("beží 1h20 · reset o 3h40"). When a source
fails, its last good numbers stay in the menu greyed out with "zastarané · pred N min" and the
reason as a ⚠ row; nothing is invented. macOS notifications: once per limit per reset window
when it crosses 80 %; when a window that was at least 30 % used resets ("okno dobehlo"); when a
source starts failing and when it is back; and when a run of at least 2 minutes has finished
("dobehol po 12 min"). The runs are polled every 30 s with `usage.py --sessions` (ps only, no
network). A Claude Code turn is read from its session log in `~/.claude/projects/`: it starts with
the first prompt after the previous `end_turn` and ends with the assistant message whose
`stop_reason` is `end_turn`; exit status of other tools is not observable, so a finished run is
reported as finished, not as failed.

Vendor marks live in `icons/` and are tinted per vendor. Claude, Gemini and Codex are the
logo shapes from [Simple Icons](https://simpleicons.org) (CC0 collection; the marks remain
their owners' trademarks). The xAI mark is the same kind of one-colour silhouette — Simple
Icons does not ship it. Without the files the app falls back to in-house vector renditions
drawn in `UsageBar.swift`.

## Install

Requires Xcode Command Line Tools (`xcode-select --install`).

```sh
git clone <this repo> ~/usagebar
~/usagebar/install.sh        # builds, registers a launchd agent, starts it
```

Update: `git pull && ./install.sh`. Remove: `./uninstall.sh`.

`./install.sh` also links `usagebar` into `/usr/local/bin`, which is already on the default macOS `PATH`. That directory is owned by root, so the installer asks for an admin password. It also links `~/.local/bin/usagebar` for shells that already search there.

## CLI

`usagebar` prints every limit, grouped by account, least-used account first. The bar is how much of that limit is used. A sample newer than five minutes is reused, so the command does not wait when the menu bar already has the numbers. The menubar's JSON output is unchanged, and this command does not post a notification.

```sh
usagebar
usagebar antigravity
usagebar claude
usagebar codex
usagebar grok
```

```text
✕ grok
  week                  1%  ▏░░░░░░░░░░░░░░░░░░░

✦ antigravity
  gemini 5h             0%  ░░░░░░░░░░░░░░░░░░░░
  gemini week           2%  ▍░░░░░░░░░░░░░░░░░░░
  other models 5h       0%  ░░░░░░░░░░░░░░░░░░░░
  other models week     0%  ░░░░░░░░░░░░░░░░░░░░

✳ claude
  5h                   11%  ██░░░░░░░░░░░░░░░░░░
  week                 44%  █████████░░░░░░░░░░░

⚛ codex
  week                 80%  ████████████████░░░░
```

`antigravity` also accepts `gemini` and `agy`. A failed fetch keeps the last good numbers and marks the line `stale`.

## Where the numbers come from

| Source | Credentials (read only, never stored by this tool) | Endpoint |
|---|---|---|
| Claude | Keychain item `Claude Code-credentials` (written by Claude Code login) | `api.anthropic.com/api/oauth/usage` — same as `/usage` in Claude Code |
| Gemini / Antigravity | the `agy` login itself: `agy -p /usage --output-format json` (agy 1.1.11+), cached 120 s | agy's own quota command — the same 5 h + weekly per-group numbers the IDE shows. A running language server is asked first only when `ANTIGRAVITY_CSRF_TOKEN` is set (agy 1.2+ rejects the localhost RPC without it). The Cloud Code endpoint is deliberately not used: it answers "100 % remaining" for everything |
| Codex | `~/.codex/auth.json` (only when the `codex` CLI is installed and logged in via ChatGPT) | `chatgpt.com/backend-api/wham/usage` — same call CodexBar makes |
| Grok | `~/.grok/auth.json` (only when the `grok` CLI is installed and logged in with `grok login`; API-key logins have no windows and are skipped). The file is read, never written. A token near expiry is renewed by `grok models`, so refresh-token rotation stays inside the CLI | `cli-chat-proxy.grok.com/v1/billing?format=credits` for the weekly window `/usage` shows, and `/v1/billing` for the monthly allowance. Override the host with `GROK_CLI_CHAT_PROXY_BASE_URL`, the same variable the CLI uses |

Tokens are sent only to their own vendor's API. Grok's token goes only to `cli-chat-proxy.grok.com`. `~/Library/Application Support/UsageBar/`
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

## Hooking up Grok

1. Install the [Grok CLI](https://x.ai/cli) (`curl -fsSL https://x.ai/cli/install.sh | bash`
   puts it on `~/.local/bin`), then `grok login`. This writes `~/.grok/auth.json`.
2. Click **Obnoviť** in the menu (or wait 5 min). A `GROK` section appears with the weekly
   credit window and, when the plan has one, the monthly allowance. The xAI mark joins the
   menubar with the weekly percent (the monthly percent, if that is the only window).
3. Nothing to configure for a normal login. `XAI_API_KEY` has no rate-limit windows and is
   skipped on purpose. A custom proxy is honoured via `GROK_CLI_CHAT_PROXY_BASE_URL`.

## Notes for other machines

Binaries are resolved by `find_binary()`, not by `PATH`: launchd starts the app with a minimal
`PATH`, so a Homebrew `codex`, a `~/.local/bin/agy` or a `~/.local/bin/grok` is invisible to
`shutil.which` and the matching section silently disappears from the menu. Add any unusual
install dir there.

## Before you publish or fork this

None of the endpoints here are documented or supported by their vendors. The tool
reads **your own** limits with **your own** login, read-only, and sends each token
only to that vendor's API. If Google, OpenAI, Anthropic or xAI change or close these endpoints the corresponding
section turns grey and shows why. Use it as a personal convenience, not as something to build a product on.

## Tweaks

| What | Where |
|---|---|
| Threshold for notifications | `NOTIFY_AT` in `usage.py` (or env `USAGEBAR_NOTIFY_AT`) |
| Minimum run length for a "dobehol" notification | `USAGEBAR_SESSION_NOTIFY_MIN` (minutes, default 2) |
| Minimum usage for an "okno dobehlo" notification | `USAGEBAR_RESET_NOTIFY_FROM` (percent, default 30) |
| Percent / elapsed flip pace in the menubar | `clockFlipSeconds` in `UsageBar.swift` |
| Subscription names | `CLAUDE_TIERS`, `codex_plan()`, `GROK_PLAN_KEYS` in `usage.py` (Keychain `rateLimitTier`, Codex id_token `chatgpt_plan_type`, any plan-like field of the Grok billing payload — untested, no Grok login on the dev machine). Antigravity exposes no tier through `agy`, so Gemini has none |
| Antigravity group labels | `ANTIGRAVITY_GROUPS` in `usage.py` |
| Which gauges appear in the menubar | `headline` in `UsageBar.swift` |
| Which window ranks a vendor in "Kam prepnúť teraz" | `limiting()` in `UsageBar.swift` (the fullest window; for Antigravity only the Gemini group) |
| Vendor marks and colours | `icons/*.svg`, `vendorGlyph()` / `vendorColor()` in `UsageBar.swift` |
| Grok billing host | `GROK_CLI_CHAT_PROXY_BASE_URL` (default `https://cli-chat-proxy.grok.com/v1`) |
| agy timeout / cache | `ANTIGRAVITY_TIMEOUT`, `ANTIGRAVITY_CACHE_TTL` in `usage.py` |
| Refresh interval | `refreshSeconds` in `UsageBar.swift` |
| Colors | `color()` in `UsageBar.swift` (green < 50, orange < 80, red ≥ 80) |

Debug the collector alone: `/usr/bin/python3 usage.py | python3 -m json.tool`.
