# Version History

## v2.6.0 (2026-10-22) - Extension version 6

**Added**
- Grok Build provider: the Grok CLI's subscription allowance (SuperGrok /
  X Premium+), the same meter its `/usage` command shows - weekly or monthly,
  as a percentage with the reset time, plus prepaid credits when there are any.
  It reads the endpoint the CLI itself uses (`cli-chat-proxy.grok.com/v1/billing`)
  and reuses the `grok login` session from `~/.grok/auth.json` (`GROK_HOME`,
  `GROK_AUTH_PATH`); there is nothing to configure. API-key logins are ignored
- the grok session lasts about 6 hours and its refresh tokens are single-use,
  so ai-usage never refreshes it. When it has expired, ai-usage runs
  `grok models` - which spends no usage - so grok renews its own session under
  its own lock, then reads it again. One renewal at a time across processes, a
  60 s timeout, and no new attempt within 10 minutes of a failed one
- OpenCode Zen provider: wallet balance and monthly pay-as-you-go spend, read
  from the authenticated workspace billing page (`OPENCODE_WORKSPACE_ID` +
  `OPENCODE_AUTH_COOKIE`); only normalized USD amounts are kept
- OpenCode Go provider: rolling 5h, weekly and monthly quota from the official
  usage endpoint, reading the `opencode-go` entry of OpenCode's `auth.json` or
  `OPENCODE_API_KEY`
- the panel can show the 30d window

**Changed**
- providers are built from one registry in `providers/__init__.py`
- the extension settings carry a provider catalog version; an upgrade makes
  visible only the providers introduced since the saved version, so a provider
  the user hid stays hidden
- the cross-process file lock used by the Claude cache moved to
  `ai_usage/file_lock.py` and is shared with the Grok renewal


## v2.5.1 (2026-09-30) - Extension version 5

**Fixed**
- Claude's usage endpoint answers 429 to more than one read every 2 minutes, and
  it was being hit far more often: every `ai-usage show` made two requests (one
  per window, though one response carries both), and the extension, the TUI and
  each CLI call fetched on their own. The response now lives in
  `~/.cache/ai-usage/claude_usage.json`, shared by every process behind a file
  lock, so the API is read at most once per 2 minutes no matter how many ask
- a 429 is shown as a rate-limit error everywhere with the retry time, and no
  process retries before the backoff ends (2 minutes, or `Retry-After` when
  longer). The cache is keyed by a hash of the token, so switching accounts
  fetches fresh data; the file holds no token


## v2.5.0 (2026-08-06) - Extension version 5

**Added**
- `install.sh` now installs the system packages it needs - git, python3 3.10+,
  python3-venv, python3-pip, pipx - through apt, dnf, pacman, zypper or apk,
  only the missing ones, printing the command it runs. Skipped with `--no-deps`
  or `AI_USAGE_SKIP_DEPS`. Without a supported package manager, or without root
  and sudo, it prints the command for the user and continues instead of aborting

## v2.4.0 (2026-08-06) - Extension version 5

**Added**
- Kimi Code (Moonshot AI) provider. Quota comes from the coding-plan
  `GET /coding/v1/usages` endpoint (falling back to `/usage`), and both response
  shapes seen in the wild are parsed. Windows are emitted as a generic
  `raw.windows` array, which both extension variants now render, so a plan with
  windows other than 5h/7d shows them as they are
- Kimi credentials are discovered from the Kimi Code CLI's `~/.kimi/config.toml`
  `[providers.*]` tables, OpenCode's `auth.json`, or Claude Code settings whose
  `ANTHROPIC_BASE_URL` is a Kimi host. Candidates are scored so a coding-plan
  `sk-kimi-…` key beats an open-platform key, which cannot read usage at all
- `tomli` is a dependency on Python 3.10 only (3.11+ has `tomllib`); without it
  the TOML source is skipped rather than breaking the package


## v2.3.0 (2026-08-06) - Extension version 5

**Fixed**
- bar fills were computed from `get_width()` in a `notify::width` handler, so a
  bar could keep a width derived from the track's CSS minimum instead of its
  final size - every bar came out proportionally short. The bar is now a
  `St.Widget` subclass that allocates its fill inside `vfunc_allocate`, using
  the box the track actually received. Measured against a screenshot, all bars
  are now within 0.5pp of their percentage (sub-pixel rounding on a 182px
  track). Drops the `notify::width` plumbing, the open-state re-apply pass and
  the bar registry along with it


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
