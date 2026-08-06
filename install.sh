#!/usr/bin/env bash
#
# AI Usage installer.
#
#   curl -fsSL https://raw.githubusercontent.com/cristianob/ai-usage-extension/main/install.sh | bash
#
# Installs the ai-usage CLI and the GNOME extension variant matching the running
# shell. Runs unattended: it never prompts, so it is safe to pipe into bash.
#
# Options (also accepted as environment variables):
#   --legacy | --modern       force an extension variant (AI_USAGE_VARIANT)
#   --no-cli                  skip the Python CLI
#   --no-extension            skip the GNOME extension
#   --ref <git-ref>           branch or tag to install (AI_USAGE_REF, default main)
#   --dir <path>              where to keep the checkout (AI_USAGE_DIR)
#   --uninstall               remove the extension, the CLI and the checkout
#
# Left alone:
#   ~/.config/ai-usage/  (keys and settings survive install and uninstall)

set -euo pipefail

REPO_URL="${AI_USAGE_REPO:-https://github.com/cristianob/ai-usage-extension.git}"
REPO_REF="${AI_USAGE_REF:-main}"
CHECKOUT_DIR="${AI_USAGE_DIR:-$HOME/.local/share/ai-usage-extension}"
VARIANT="${AI_USAGE_VARIANT:-auto}"

EXT_UUID="ai-usage@local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

WANT_CLI=1
WANT_EXTENSION=1
UNINSTALL=0

# ---------------------------------------------------------------- output ----

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; RESET=''
fi

step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$RESET"; }
warn() { printf '    %s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
ok()   { printf '    %s* %s%s\n' "$GREEN" "$*" "$RESET"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ----------------------------------------------------------------- args -----

while [[ $# -gt 0 ]]; do
    case "$1" in
        --legacy)       VARIANT="extension_legacy" ;;
        --modern)       VARIANT="extension" ;;
        --no-cli)       WANT_CLI=0 ;;
        --no-extension) WANT_EXTENSION=0 ;;
        --uninstall)    UNINSTALL=1 ;;
        --ref)          REPO_REF="${2:?--ref needs a value}"; shift ;;
        --dir)          CHECKOUT_DIR="${2:?--dir needs a value}"; shift ;;
        -h|--help)
            # Print the header comment block, minus the shebang.
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0
            ;;
        *)              die "unknown option: $1" ;;
    esac
    shift
done

# ------------------------------------------------------------- uninstall ----

if [[ "$UNINSTALL" == 1 ]]; then
    step "Removing the GNOME extension"
    if have gnome-extensions; then
        gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    fi
    rm -rf "$EXT_DIR"
    ok "$EXT_DIR"

    step "Removing the CLI"
    if have pipx && pipx list 2>/dev/null | grep -q 'package ai-usage'; then
        pipx uninstall ai-usage >/dev/null && ok "pipx package ai-usage"
    elif have pip3; then
        pip3 uninstall -y ai-usage >/dev/null 2>&1 && ok "pip package ai-usage" || note "no pip install found"
    fi

    step "Removing the checkout"
    rm -rf "$CHECKOUT_DIR"
    ok "$CHECKOUT_DIR"

    note "Kept ~/.config/ai-usage (keys and settings)"
    exit 0
fi

# ---------------------------------------------------------------- source ----

SCRIPT_DIR=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/extension" ]]; then
    # Running from a checkout (./install.sh): use it as-is, no network needed.
    SOURCE_DIR="$SCRIPT_DIR"
    step "Using this checkout"
    info "$SOURCE_DIR"
else
    have git || die "git is required to fetch the sources"
    SOURCE_DIR="$CHECKOUT_DIR"

    if [[ -d "$SOURCE_DIR/.git" ]]; then
        step "Updating $SOURCE_DIR"
        git -C "$SOURCE_DIR" fetch --quiet origin "$REPO_REF"
        git -C "$SOURCE_DIR" checkout --quiet FETCH_HEAD
    else
        step "Cloning $REPO_URL"
        mkdir -p "$(dirname "$SOURCE_DIR")"
        git clone --quiet --depth 1 --branch "$REPO_REF" "$REPO_URL" "$SOURCE_DIR" \
            || die "clone failed - is the repository reachable and is '$REPO_REF' a valid ref?"
    fi
    ok "$(git -C "$SOURCE_DIR" log --oneline -1)"
fi

[[ -d "$SOURCE_DIR/extension" && -d "$SOURCE_DIR/extension_legacy" ]] \
    || die "$SOURCE_DIR does not look like an ai-usage checkout"

# ------------------------------------------------------------------- CLI ----

install_cli() {
    step "Installing the ai-usage CLI"

    have python3 || die "python3 is required"

    local python_ok
    python_ok="$(python3 - <<'PY'
import sys
print("yes" if sys.version_info >= (3, 10) else f"no {sys.version_info.major}.{sys.version_info.minor}")
PY
)"
    [[ "$python_ok" == "yes" ]] || die "python 3.10+ is required (found ${python_ok#no })"

    if have pipx; then
        pipx install --force "$SOURCE_DIR" >/dev/null
        ok "pipx install ai-usage"
    else
        warn "pipx not found, falling back to a private virtualenv"
        local venv="$HOME/.local/share/ai-usage/venv"
        python3 -m venv "$venv" >/dev/null 2>&1 || die "could not create $venv (python3-venv installed?)"
        "$venv/bin/pip" install --quiet --upgrade pip >/dev/null
        "$venv/bin/pip" install --quiet "$SOURCE_DIR" || die "pip install failed"
        mkdir -p "$HOME/.local/bin"
        ln -sf "$venv/bin/ai-usage" "$HOME/.local/bin/ai-usage"
        ok "$HOME/.local/bin/ai-usage -> $venv"
        note "Install pipx for easier upgrades: python3 -m pip install --user pipx"
    fi

    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) warn "~/.local/bin is not in PATH - add it to your shell profile" ;;
    esac

    if have ai-usage; then
        info "$(ai-usage --version 2>/dev/null || echo 'ai-usage installed')"
    fi
}

# ------------------------------------------------------------- extension ----

shell_major() {
    gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -1
}

install_extension() {
    step "Installing the GNOME extension"

    local variant="$VARIANT"
    if [[ "$variant" == "auto" ]]; then
        local major
        major="$(shell_major)"
        if [[ -z "$major" ]]; then
            warn "could not detect the GNOME Shell version, assuming 45+"
            warn "re-run with --legacy if you are on GNOME 42-44"
            variant="extension"
        elif [[ "$major" -ge 45 ]]; then
            variant="extension"
            info "GNOME Shell $major -> modern build"
        else
            variant="extension_legacy"
            info "GNOME Shell $major -> legacy build"
        fi
    else
        info "forced variant: $variant"
    fi

    rm -rf "$EXT_DIR"
    mkdir -p "$EXT_DIR"
    cp -r "$SOURCE_DIR/$variant"/. "$EXT_DIR"/
    ok "$EXT_DIR"

    if have gnome-extensions; then
        # A brand new UUID is invisible until the shell rescans, so a failure
        # here is expected on a first install and not fatal.
        if gnome-extensions enable "$EXT_UUID" 2>/dev/null; then
            ok "extension enabled"
        else
            note "enable it after the shell restart: gnome-extensions enable $EXT_UUID"
        fi
    fi
}

[[ "$WANT_CLI" == 1 ]] && install_cli
[[ "$WANT_EXTENSION" == 1 ]] && install_extension

# ------------------------------------------------------------ next steps ----

step "Next steps"

if [[ "$WANT_EXTENSION" == 1 ]]; then
    major="$(shell_major || true)"
    if [[ -n "${major:-}" && "$major" -lt 45 && "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
        info "Restart the shell: Alt+F2, type 'r', Enter"
    else
        info "Log out and back in to load the extension"
    fi
    info "Then: gnome-extensions prefs $EXT_UUID"
fi

if [[ "$WANT_CLI" == 1 ]]; then
    info "Check the providers: ai-usage doctor"
    info "Add keys graphically, or run: ai-usage setup"
fi

note "Uninstall with: $SOURCE_DIR/install.sh --uninstall"
