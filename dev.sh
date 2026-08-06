#!/usr/bin/env bash
# Install / manage the AI Usage GNOME extension.
#
# Two implementations live side by side because GNOME 45 changed the extension
# module system:
#
#   extension/         GNOME 45+   ES modules, Extension subclass, libadwaita 1.4 rows
#   extension_legacy/  GNOME 42-44 legacy imports, init(), libadwaita 1.1 rows
#
# "install" picks the right one for the running shell. Override with
# --legacy / --modern when installing for a different machine.

set -euo pipefail

EXT_UUID="ai-usage@local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

shell_major() {
    local version
    version="$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -1)"
    if [[ -z "$version" ]]; then
        echo "Could not detect the GNOME Shell version." >&2
        echo "Pass --legacy or --modern explicitly." >&2
        exit 1
    fi
    echo "$version"
}

pick_variant() {
    case "${1:-auto}" in
        --legacy) echo "extension_legacy" ;;
        --modern) echo "extension" ;;
        *)
            if [[ "$(shell_major)" -ge 45 ]]; then
                echo "extension"
            else
                echo "extension_legacy"
            fi
            ;;
    esac
}

do_install() {
    local variant
    variant="$(pick_variant "${1:-auto}")"
    local src="$REPO_DIR/$variant"

    if [[ ! -f "$src/extension.js" ]]; then
        echo "Missing $src/extension.js" >&2
        exit 1
    fi

    rm -rf "$EXT_DIR"
    mkdir -p "$EXT_DIR"
    cp -r "$src"/. "$EXT_DIR"/

    echo "Installed $variant -> $EXT_DIR"
    if [[ "$variant" == "extension_legacy" ]]; then
        echo "Reload with: Alt+F2, then r  (X11) or log out and back in (Wayland)"
    else
        echo "Reload by logging out and back in (GNOME 45+ has no in-place restart)"
    fi
}

case "${1:-}" in
    install)
        do_install "${2:-auto}"
        ;;
    variant)
        pick_variant "${2:-auto}"
        ;;
    start)
        dbus-run-session -- gnome-shell --nested --wayland
        ;;
    enable)
        gnome-extensions enable "$EXT_UUID"
        ;;
    disable)
        gnome-extensions disable "$EXT_UUID"
        ;;
    reload)
        gnome-extensions disable "$EXT_UUID"
        sleep 1
        gnome-extensions enable "$EXT_UUID"
        ;;
    prefs)
        gnome-extensions prefs "$EXT_UUID"
        ;;
    logs)
        journalctl -f -o cat /usr/bin/gnome-shell | grep -i "ai-usage"
        ;;
    *)
        cat <<'EOF'
Usage: ./dev.sh <command> [--legacy|--modern]

Commands:
  install   Copy the right variant into ~/.local/share/gnome-shell/extensions
  variant   Print which variant would be installed
  enable    Enable the extension
  disable   Disable the extension
  reload    Disable + enable (picks up JS changes on X11)
  prefs     Open the preferences window
  start     Run a nested GNOME Shell for testing
  logs      Follow the extension's log output
EOF
        exit 1
        ;;
esac
