#!/bin/bash
# Development helper for the AI Usage GNOME extension

EXT_UUID="ai-usage@local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$1" in
    install)
        mkdir -p "$EXT_DIR"
        cp -r "$SRC_DIR"/extension.js "$SRC_DIR"/prefs.js "$SRC_DIR"/stylesheet.css \
              "$SRC_DIR"/metadata.json "$SRC_DIR"/icons "$EXT_DIR"/
        echo "Installed to $EXT_DIR"
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
        echo "Usage: $0 {install|start|enable|disable|reload|prefs|logs}"
        exit 1
        ;;
esac
