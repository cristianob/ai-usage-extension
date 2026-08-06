# AI Usage

**AI provider quotas in a GNOME panel indicator and a terminal UI.**
Claude, Codex, Z.ai, Kimi Code and OpenRouter, side by side.

Two pieces:

- **AI Usage Extension** — GNOME Shell panel indicator, one card per provider
- **AI Usage TUI** — the `ai-usage` CLI/TUI (Python, Textual)

Works on **GNOME 42 through 50**. The extension ships two implementations
because GNOME 45 replaced the extension module system; the installer picks the
right one. The Python side is shared by both.

This is a decoupled fork of GnomeCodexBar, maintained independently.

---

## Quick start

```bash
# 1. install the CLI and the extension build for your GNOME version
curl -fsSL https://raw.githubusercontent.com/cristianob/ai-usage-extension/main/install.sh | bash

# 2. load the extension
#    GNOME 42-44 on X11:   Alt+F2, type r, Enter
#    Wayland or GNOME 45+: log out and back in

# 3. add your API keys
gnome-extensions prefs ai-usage@local
```

The installer never prompts, so it is safe to pipe; only `sudo` may ask for a
password, and only if a system package is actually missing. It puts the checkout
in `~/.local/share/ai-usage-extension` (re-running it updates), and installs the
CLI with `pipx` when available, otherwise into a private virtualenv symlinked
into `~/.local/bin`.

**What it installs.** System packages, through `apt`, `dnf`, `pacman`, `zypper`
or `apk`: `git`, `python3` (3.10+), `python3-venv`, `python3-pip` and `pipx` —
only the ones missing, and it prints the command it runs. Python libraries
(`textual`, `httpx`, `click`, `pydantic`, and `tomli` on 3.10) come with the CLI
itself. Without a supported package manager, or without root and `sudo`, it
prints the exact command for you to run and carries on.

### Installer options

```bash
# read it first if you would rather not pipe a script into bash
curl -fsSL .../install.sh -o install.sh && less install.sh && bash install.sh

bash install.sh --no-deps       # never touch system packages
bash install.sh --legacy        # force the GNOME 42-44 build
bash install.sh --modern        # force the GNOME 45+ build
bash install.sh --no-cli        # extension only
bash install.sh --no-extension  # CLI only
bash install.sh --ref <tag>     # a specific tag or branch
bash install.sh --uninstall     # remove everything except ~/.config/ai-usage
```

<details>
<summary>From a clone instead</summary>

```bash
git clone https://github.com/cristianob/ai-usage-extension.git
cd ai-usage-extension
./install.sh            # same script, uses this checkout, no network needed
# or, piecemeal:
pipx install --force .  # the CLI
./dev.sh install        # the extension, variant auto-detected
./dev.sh enable
```
</details>

### Check the backend from a terminal

```bash
ai-usage doctor      # per-provider config + connectivity
ai-usage show        # formatted output
ai-usage show --json # what the extension consumes
ai-usage tui         # full TUI
```

---

## Supported providers

| Provider | Windows | Auth |
|----------|---------|------|
| Claude Code | 5h + weekly | Claude CLI credentials, or `CLAUDE_CODE_OAUTH_TOKEN` |
| OpenAI Codex | weekly only | `~/.codex/auth.json`, or `CODEX_ACCESS_TOKEN` |
| Z.ai / ZCode (GLM Coding Plan) | 5h + weekly | ZCode / OpenCode / Claude Code credentials, or `ZAI_API_KEY` |
| Kimi Code (Moonshot AI) | whatever the plan has | Kimi CLI / OpenCode / Claude Code credentials, or `KIMI_API_KEY` |
| OpenRouter | spend-limit window | `OPENROUTER_API_KEY` |

Codex dropped its 5 hour window: the ChatGPT backend now returns a single
`primary_window` covering 604800 seconds, so only a weekly bar is shown. Window
labels are always derived from the data, never from a provider's position.

### Kimi Code credential discovery

Same idea, probed in order:

1. `KIMI_API_KEY` / `KIMI_CODING_API_KEY` (env var or `~/.config/ai-usage/env`)
2. **Kimi Code CLI** — `~/.kimi/config.toml` (also `~/.kimi-code/` and the
   `~/.config/` layouts), reading the `[providers.*]` tables
3. **OpenCode** — `~/.local/share/opencode/auth.json`
4. **Claude Code** — `~/.claude/settings.json` when `ANTHROPIC_BASE_URL` points
   at a Kimi host

It has to be a Kimi Code console key (`sk-kimi-…`); an open-platform key
(`sk-…`, `api.moonshot.cn`) cannot read the usage endpoint, so candidates
advertising the open platform lose to the ones that do not. Quota comes from
`GET /coding/v1/usages` (older builds answer on `/usage`), and whatever windows
the plan reports are rendered as-is instead of being forced into 5h/7d slots.
`KIMI_BASE_URL` overrides the base URL.

### Z.ai credential discovery

Like Claude and Codex, Z.ai needs no key of its own when another client already
holds one. Probed in order:

1. `ZAI_API_KEY` / `ZHIPU_API_KEY` (env var or `~/.config/ai-usage/env`)
2. **ZCode** — `~/.zcode/v2/config.json`
   (`provider."builtin:zai-coding-plan".options.apiKey`), plus
   `~/.zcode/v2/credentials.json` and the `~/.config/zcode/` layout
3. **OpenCode** — `~/.local/share/opencode/auth.json` (`zai-coding-plan`, `zai`,
   `zhipu`, …)
4. **Claude Code** — `~/.claude/settings.json` when `ANTHROPIC_BASE_URL` points
   at `api.z.ai` or `open.bigmodel.cn` (the GLM-in-Claude-Code setup)

ZCode encrypts its OAuth tokens (`enc:v1:…`, Electron safeStorage); those values
are rejected instead of being sent to the API. Its coding-plan **API key** is
stored in plaintext, so discovery works anyway. Endpoints, model ids and status
strings are filtered out, and candidates are scored so the real key wins over a
session JWT or an inactive plan entry.

The Claude Code source is only trusted when the base URL really is a Z.ai host,
so an Anthropic token is never sent to Z.ai. The platform (global vs. CN) is
inferred from where the credential was found; `ZAI_PLATFORM` overrides it.

An account without an active plan gets HTTP 200 with an in-body error code,
surfaced as `No active coding plan for this key`.

---

## The extension

UUID `ai-usage@local`. One card per provider, one progress bar per quota window,
with the reset countdown under each bar.

The panel number comes from the pinned provider: the 5h window, the 7d window,
or both separated by a bullet (`46% · 59%`). Providers with a single window show
that one; money-based providers (OpenRouter) show the amount spent.

The popup itself holds only **Refresh Now** and **Settings & API Keys** — every
option lives in the preferences window.

### Preferences

`./dev.sh prefs`, or *Settings & API Keys* in the popup:

- **API Keys** — a password field per provider key, written to
  `~/.config/ai-usage/env` (mode 600, comments and unknown lines preserved), the
  Z.ai platform selector, and buttons to run the Claude/Codex CLI logins
- **Display** — pinned provider, which window the panel shows, refresh interval,
  plus per-provider visibility switches and reordering arrows
- **Backend** — the resolved CLI path, and buttons to run `ai-usage doctor` or
  open the TUI

Changing an option re-renders from cached data; only a changed credential
triggers a new CLI call. The extension watches both files, so nothing needs a
shell reload.

### Two implementations

| Directory | Shell | Module system | Preferences widgets |
|-----------|-------|---------------|---------------------|
| `extension/` | 45–50 | ES modules, `Extension` subclass | libadwaita 1.4 `PasswordEntryRow` / `SwitchRow` / `SpinRow` |
| `extension_legacy/` | 42–44 | legacy `imports`, `init()` | libadwaita 1.1 `ActionRow` + GTK widgets |

The two are genuinely separate implementations, not a shim: a single file cannot
satisfy both module systems. They are kept deliberately close — apart from the
imports, the extension entry point and the preferences widget classes, the logic
is identical, so a behaviour change belongs in **both**.

Each directory is self-contained (`extension.js`, `prefs.js`, `metadata.json`,
`stylesheet.css`, `icons/`), so it can also be copied straight into
`~/.local/share/gnome-shell/extensions/ai-usage@local/` by hand.

```bash
./dev.sh variant            # which one would be installed here
./dev.sh install --legacy   # force the GNOME 42-44 build
./dev.sh install --modern   # force the GNOME 45+ build
./dev.sh logs               # follow the extension's output
./dev.sh start              # nested shell for testing
```

---

## Configuration files

| Path | Purpose |
|------|---------|
| `~/.config/ai-usage/env` | provider keys, `KEY=value` lines, mode 600 |
| `~/.config/ai-usage/extension.json` | pinned provider, panel window, provider order and visibility, refresh interval |
| `~/.cache/ai-usage/` | cached provider results |

---

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

---

## Development

```bash
pip install -e '.[dev]'
ruff check .
mypy ai_usage
```

Python 3.10 needs the `tomli` backport for the Kimi CLI's TOML config; 3.11+ uses
the standard library's `tomllib`. Both are handled by the dependency list, and if
neither is importable the TOML credential source is skipped rather than failing.

Adding a provider means a class in `ai_usage/providers/` registered in
`providers/__init__.py`, `config.py` and `cli.py`, then its name added to
`PROVIDER_ORDER` in **both** extension variants (plus an icon and two CSS rules).

## License

MIT (see `LICENSE`).
