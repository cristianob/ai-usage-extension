
Focus: rules for usage and testing of features.

Project identity
- Local, decoupled fork of GnomeCodexBar. Do not add an upstream remote.
- CLI/TUI is "AI Usage TUI" (`ai-usage`, package `ai_usage`).
- GNOME extension is "AI Usage Extension" (uuid `ai-usage@local`).

Layout
- `ai_usage/` - Python CLI/TUI, shared by every shell version.
- `extension/` - GNOME 45-50 build: ES modules, `Extension` subclass,
  libadwaita 1.4 rows.
- `extension_legacy/` - GNOME 42-44 build: legacy `imports`, `init()`,
  libadwaita 1.1 rows.
- `dev.sh` - installs the variant matching the running shell.

Extension rules
- The two variants are separate implementations because GNOME 45 changed the
  module system. A behaviour change goes in BOTH; keep them line-for-line
  similar so `diff extension_legacy/extension.js extension/extension.js` stays
  small and reviewable (only imports, the entry point, the extension-object
  plumbing).
- Each variant directory is self-contained: `extension.js`, `prefs.js`,
  `metadata.json`, `stylesheet.css`, `icons/`. Keep the shared files in sync;
  only `metadata.json` differs, in `shell-version`.
- Progress bar widths depend on real allocation, so verify with the popup open,
  not just by reading the code.
- Options must never trigger a data refetch. Only a credential change does.

Manual smoke checks
- ai-usage show
- ai-usage show --provider claude
- ai-usage show --provider codex
- ai-usage show --provider zai
- ai-usage show --json
- ai-usage doctor
- ai-usage env
- ai-usage tui
- ai-usage login --provider claude
- ai-usage refresh --provider all
- ./token_refresh.sh once
- ./dev.sh variant && ./dev.sh install && ./dev.sh reload && ./dev.sh prefs

Syntax checks without a shell restart
- legacy: `node --check extension_legacy/extension.js` (and prefs.js)
- modern: copy to a `.mjs` first, then `node --check` (it is an ES module)
- prefs can be built headless in gjs by instantiating an `Adw.PreferencesWindow`
  and calling into the file's entry point; this only works for the variant whose
  libadwaita version the host provides.

Testing rules
- Run ai-usage show and ai-usage tui for UI/CLI changes.
- For extension changes, install + reload the shell and open the popup.

Versioning rules
- ALWAYS read VERSIONLOG.md before making version changes or creating releases
- Extension version in metadata.json is an integer (GNOME requirement), git tags
  use semantic versioning; keep the integer identical in both variants
- Update VERSIONLOG.md with changes whenever bumping versions
- ai-usage uses standard semantic versioning (1.0.0, 1.0.1, etc.)

Commit rules
- Before committing, check for env files and secrets. Never commit a real key;
  `.env.example` holds placeholders only.
