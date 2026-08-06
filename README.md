# AI Usage

**Unified AI usage metrics in a fast terminal UI and a GNOME panel indicator.**
Claude, Codex, Z.ai and OpenRouter, side by side.

This is a decoupled local fork of GnomeCodexBar. It has no upstream remote and is
maintained only here.

Two pieces:

- **AI Usage TUI** — the `ai-usage` CLI/TUI (Python, Textual)
- **AI Usage Extension** — the GNOME Shell panel indicator in `extension/`

## Features

- Interactive TUI built with Textual
- Unified dashboard across providers
- Scriptable CLI with JSON output
- Cached requests to reduce API calls
- GNOME Shell panel indicator with per-window progress bars
- Graphical preferences for API keys (no editing files by hand)
- Multiple time windows (5h, 7d, 30d) where the provider offers them

## Supported providers

| Provider | Windows | Auth |
|----------|---------|------|
| Claude Code | 5h + weekly | Claude CLI credentials, or `CLAUDE_CODE_OAUTH_TOKEN` |
| OpenAI Codex | weekly only | `~/.codex/auth.json`, or `CODEX_ACCESS_TOKEN` |
| Z.ai / ZCode (GLM Coding Plan) | 5h + weekly | ZCode / OpenCode / Claude Code credentials, or `ZAI_API_KEY` |
| OpenRouter | credit limit window | `OPENROUTER_API_KEY` |

Codex dropped its 5 hour window: the ChatGPT backend now returns a single
`primary_window` covering 604800 seconds, so only a weekly bar is shown.

### Z.ai credential discovery

Like Claude and Codex, Z.ai needs no key of its own when another client already
holds one. Probed in order:

1. `ZAI_API_KEY` / `ZHIPU_API_KEY` (env var or `~/.config/ai-usage/env`)
2. ZCode — `~/.zcode/v2/config.json` (`provider."builtin:zai-coding-plan".options.apiKey`),
   plus `~/.zcode/v2/credentials.json` and the `~/.config/zcode/` layout
3. OpenCode — `~/.local/share/opencode/auth.json` (`zai-coding-plan`, `zai`, `zhipu`, ...)
4. Claude Code — `~/.claude/settings.json` when `ANTHROPIC_BASE_URL` points at
   `api.z.ai` or `open.bigmodel.cn` (the GLM-in-Claude-Code setup)

The Claude Code source is only trusted when the base URL really is a Z.ai host,
so an Anthropic token is never sent to Z.ai. The platform (global vs. CN) is
inferred from where the credential was found, and `ZAI_PLATFORM` overrides it.

ZCode encrypts its OAuth tokens (`enc:v1:…`, Electron safeStorage); those values
are rejected instead of being sent to the API. The coding-plan **API key** is
stored in plaintext, so discovery works anyway. Endpoints, model ids and status
strings are filtered out, and candidates are scored so the real key wins over a
session JWT or an inactive plan entry.

If the account has no active plan the endpoint answers HTTP 200 with an in-body
error code; that is surfaced as `No active coding plan for this key`. The raw
provider message is kept in the JSON `raw` payload only.

## Quick start

```bash
# install the CLI (pipx recommended)
pipx install --force ~/Git/ai-usage-extension

# configure keys interactively
ai-usage setup

# check everything is wired up
ai-usage doctor

# one-shot output
ai-usage show
ai-usage show --json

# full TUI
ai-usage tui
```

`usage-tui` remains as an alias for `ai-usage` so older launchers keep working.

## GNOME Shell extension

```bash
extension/dev.sh install
extension/dev.sh enable
extension/dev.sh prefs     # graphical key configuration
extension/dev.sh logs
```

UUID: `ai-usage@local`. Supported shell versions: 42–44.

The indicator shows one progress bar per quota window per provider. The pinned
panel number is always the **5h** window; providers without a 5h window (Codex)
show their shortest window with its label, e.g. `7d 41%`.

### Preferences

`extension/dev.sh prefs` (or *Settings & API Keys* in the popup menu) opens a
GTK/libadwaita window with:

- password fields for every provider key, saved to `~/.config/ai-usage/env` (mode 600)
- Z.ai platform selector (global vs. CN)
- pinned provider and refresh interval
- buttons to run `ai-usage doctor`, open the TUI, and run the Claude/Codex CLI logins

The extension watches `~/.config/ai-usage/extension.json`, so changes apply
without reloading the shell.

## Configuration files

| Path | Purpose |
|------|---------|
| `~/.config/ai-usage/env` | provider keys (`KEY=value` lines) |
| `~/.config/ai-usage/extension.json` | panel provider, refresh interval |
| `~/.cache/ai-usage/` | cached provider results |

The pre-rename `~/.config/usage-tui/` locations are still read when the new ones
do not exist, so nothing breaks on upgrade; the first write lands in the new path.

## Token refresh daemon

`token_refresh.sh` keeps long-lived credentials alive. Claude's are renewed by
poking the Claude CLI; Codex's are refreshed against OpenAI's token endpoint and
written back to `auth.json`.

```bash
./token_refresh.sh once            # one cycle, both providers
./token_refresh.sh start codex     # daemonize, Codex only
./token_refresh.sh status
./token_refresh.sh stop
```

Interval: `AI_USAGE_REFRESH_INTERVAL_SECONDS` (default 1800). The same work is
available directly as `ai-usage refresh [--provider claude|codex|all] [--force]`.

## Development

```bash
pip install -e '.[dev]'
ruff check .
mypy ai_usage
```

## License

MIT (see `LICENSE`).
