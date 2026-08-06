
Focus: rules for usage and testing of features.

Project identity
- Local, decoupled fork of GnomeCodexBar. No upstream remote; do not add one.
- CLI/TUI is "AI Usage TUI" (`ai-usage`, package `ai_usage`).
- GNOME extension is "AI Usage Extension" (uuid `ai-usage@local`).

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
- extension/dev.sh install && extension/dev.sh reload && extension/dev.sh prefs

Testing rules
- Run ai-usage show and ai-usage tui for UI/CLI changes.
- For extension changes, install + reload the shell and open the popup, since bar
  widths depend on real allocation.

Versioning rules
- ALWAYS read VERSIONLOG.md before making version changes or creating releases
- Extension version in metadata.json is an integer (GNOME requirement), git tags use semantic versioning
- Update VERSIONLOG.md with changes whenever bumping versions
- ai-usage uses standard semantic versioning (1.0.0, 1.0.1, etc.)

Commit rules
- Before committing, check for env files and secrets, then follow the git-workflow skill.
