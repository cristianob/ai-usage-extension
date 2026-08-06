# Version History

## v2.2.0 (2026-08-06) - Extension version 4

**Added**
- `install.sh`, curl-pipeable: installs the CLI and the extension variant
  matching the running shell, reuses the surrounding checkout when run from a
  clone, and has `--legacy` / `--modern` / `--no-cli` / `--no-extension` /
  `--ref` / `--dir` / `--uninstall`

**Removed**
- every `usage-tui` compatibility path. The alias console script is gone (only
  `ai-usage` remains), the extension and its preferences no longer look for a
  `usage-tui` binary, a `USAGE_TUI_PATH` override, or config under
  `~/.config/usage-tui/`, and that directory has been deleted after migration

## v2.1.0 (2026-08-06) - Extension version 4

**Added**
- GNOME 45-50 support. GNOME 45 replaced the extension module system, so the
  extension now ships two implementations: `extension/` (ES modules, `Extension`
  subclass, libadwaita 1.4 `PasswordEntryRow`/`SwitchRow`/`SpinRow`) and
  `extension_legacy/` (legacy `imports`, `init()`, libadwaita 1.1 rows). A
  single file cannot satisfy both module systems, so they are separate on
  purpose and kept line-for-line similar
- `dev.sh` moved to the repository root and now detects the running shell to
  install the right variant, with `--legacy` / `--modern` overrides and a
  `variant` command
- each variant directory is self-contained, so it can be copied into
  `~/.local/share/gnome-shell/extensions/ai-usage@local/` by hand

**Changed**
- both variants log through `console.log` instead of the deprecated `log()`
- docs rewritten around the dual-variant layout and the shared Python backend

## v2.0.0 (2026-08-06) - Extension version 4

Renamed and decoupled: the project is now **AI Usage Extension** (uuid
`ai-usage@local`) plus **AI Usage TUI** (`ai-usage`, package `ai_usage`).
The upstream remote was removed; this repository is local-only.

**Added**
- Z.ai (GLM Coding Plan) provider: 5h + weekly token quota via
  `/api/monitor/usage/quota/limit`, global and CN platforms
- Graphical preferences (`prefs.js`): password fields for every provider key,
  Z.ai platform selector, pinned provider, refresh interval, and buttons to run
  doctor / the TUI / the Claude and Codex CLI logins
- Extension watches `~/.config/ai-usage/extension.json` and applies changes live
- `ai-usage` console script (with `usage-tui` kept as an alias)

**Fixed**
- Progress bar widths were computed from a single `get_width()` read in an idle
  callback, which raced the first allocation: bars rendered tiny or empty. Fills
  now track `notify::width` on their background and are re-applied when the menu
  opens.
- The pinned panel value is always the 5h window (it used to take the maximum of
  all windows). Providers without a 5h window show their shortest window with a
  label, e.g. `7d 41%`.
- Codex is weekly-only now that its backend returns a single 604800 s
  `primary_window`; window labels are derived from `limit_window_seconds`
  instead of assuming primary = 5h.
- Percentage-based quotas are no longer printed as "41.0 / 100.0 credits".

- Z.ai credentials are discovered from ZCode / OpenCode / Claude Code instead of
  requiring a pasted key (ZCode's plaintext coding-plan `apiKey`; its encrypted
  `enc:v1:` OAuth tokens are rejected, and candidates are scored so endpoints,
  status strings and session JWTs never win)
- Panel window is configurable: 5h, 7d, or both separated by a bullet
  (`46% · 59%`); single-window providers show their one window and money-based
  providers (OpenRouter) show the amount spent
- `ai-usage refresh` command and a generalized `token_refresh.sh` that also
  refreshes Codex tokens (was Claude-only)

**Removed**
- GitHub Copilot provider, its device-flow login and credential store
- OpenAI platform provider (`OPENAI_ADMIN_KEY`)

## Extension Versions

### v1.0.1 (2026-02-15) - Extension version 3
**Fixed**
- Copilot provider now displays actual credit numbers (e.g., 162/1500) instead of percentages (10.8/100)
- Improved quota tracking accuracy for all Copilot plans (Free, Pro, Business)

**Technical Changes**
- Modified `_extract_quota_data()` to prioritize extracting `entitlement` and `quota_remaining` from API
- Falls back to percentage-based metrics only when actual credits unavailable
- Extension metadata version: 3

### v1.0.0 (Initial Release) - Extension version 2
**Added**
- Initial release of Usage TUI Monitor for GNOME Shell
- Monitor Claude, OpenAI, OpenRouter, Copilot, and Codex usage
- Real-time quota tracking in GNOME top panel
- Color-coded provider indicators
- Auto-refresh with configurable interval
- Extension metadata version: 2

---

## Python Package Versions (released as `usage-tui` before v2.0.0)

### v0.1.1 (2026-02-15)
**Fixed**
- Copilot provider now correctly extracts and displays actual credit numbers from API response
- Store actual credit values in `metrics.remaining` and `metrics.limit` instead of percentages

### v0.1.0 (Initial Release)
**Added**
- Multi-provider usage metrics TUI
- Support for Claude, OpenAI, OpenRouter, GitHub Copilot, and Codex
- Interactive TUI with multiple time windows (5h, 7d, 30d)
- JSON output for scripting
- Caching layer to reduce API calls
- OAuth device flow for Copilot authentication

---

## Version Numbering

- **Extension**: semantic versioning for git tags, integer versions in metadata.json
  per GNOME requirements; both variants carry the same integer
- **ai-usage** (Python package): semantic versioning
