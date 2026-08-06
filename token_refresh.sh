#!/usr/bin/env bash
#
# token_refresh.sh - Background helper to keep provider tokens fresh
#
# Usage: ./token_refresh.sh {start|stop|status|once|loop} [provider]
#
#   start  - Daemonize and run refresh loop in background
#   stop   - Stop the running daemon
#   status - Check if daemon is running
#   once   - Run a single refresh immediately
#   loop   - Run refresh loop forever (foreground)
#
#   provider - claude, codex or all (default: all)
#
# Environment:
#   XDG_CONFIG_HOME                    Config directory (default: ~/.config)
#   AI_USAGE_REFRESH_INTERVAL_SECONDS  Interval in seconds (default: 1800)

set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ai-usage"
PID_FILE="$CONFIG_DIR/token_refresh.pid"
LOG_FILE="$CONFIG_DIR/token_refresh.log"
INTERVAL="${AI_USAGE_REFRESH_INTERVAL_SECONDS:-1800}"
PROVIDER="${2:-all}"

mkdir -p "$CONFIG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

cli() {
    if command -v ai-usage >/dev/null 2>&1; then
        echo "ai-usage"
    elif command -v usage-tui >/dev/null 2>&1; then
        echo "usage-tui"
    else
        echo ""
    fi
}

# Claude's credentials belong to the Claude CLI: touching /usage makes it renew
# them. Codex tokens are refreshed by our own refresh command.
refresh_claude() {
    if command -v claude >/dev/null 2>&1; then
        log "Running: claude /usage"
        claude /usage >>"$LOG_FILE" 2>&1 || log "Warning: claude /usage failed"
    else
        log "Warning: claude command not found"
    fi
}

do_refresh() {
    local bin
    bin="$(cli)"

    log "Starting token refresh cycle (provider: $PROVIDER)"

    if [[ "$PROVIDER" == "claude" || "$PROVIDER" == "all" ]]; then
        refresh_claude
    fi

    if [[ -z "$bin" ]]; then
        log "Warning: ai-usage command not found"
    else
        log "Running: $bin refresh --provider $PROVIDER"
        "$bin" refresh --provider "$PROVIDER" >>"$LOG_FILE" 2>&1 \
            || log "Warning: $bin refresh reported a problem"
    fi

    log "Token refresh cycle complete"
}

run_loop() {
    log "Starting refresh loop (interval: ${INTERVAL}s)"
    while true; do
        do_refresh
        log "Sleeping for ${INTERVAL}s"
        sleep "$INTERVAL"
    done
}

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null) || return 1
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

start_daemon() {
    if is_running; then
        log "Daemon already running (PID: $(cat "$PID_FILE"))"
        return 0
    fi

    log "Starting daemon"
    nohup "$0" loop "$PROVIDER" >>"$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" >"$PID_FILE"
    log "Daemon started (PID: $pid)"
}

stop_daemon() {
    if ! is_running; then
        log "Daemon not running"
        rm -f "$PID_FILE"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    log "Stopping daemon (PID: $pid)"

    if kill "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        log "Daemon stopped"
    else
        log "Failed to stop daemon"
        return 1
    fi
}

show_status() {
    if is_running; then
        echo "Daemon is running (PID: $(cat "$PID_FILE"))"
    else
        echo "Daemon is not running"
    fi
    echo "Config directory:  $CONFIG_DIR"
    echo "Log file:          $LOG_FILE"
    echo "Refresh interval:  ${INTERVAL}s"
    echo "CLI:               $(cli 2>/dev/null || echo 'not found')"
    echo
    if [[ -n "$(cli)" ]]; then
        "$(cli)" refresh --provider all || true
    fi
}

show_usage() {
    cat <<'EOF'
Usage: ./token_refresh.sh {start|stop|status|once|loop} [provider]

Commands:
  start  - Daemonize and run refresh loop in background
  stop   - Stop the running daemon
  status - Check if daemon is running and report token state
  once   - Run a single refresh immediately
  loop   - Run refresh loop forever (foreground)

Provider:
  claude | codex | all   (default: all)

Environment Variables:
  XDG_CONFIG_HOME                    Config directory (default: ~/.config)
  AI_USAGE_REFRESH_INTERVAL_SECONDS  Interval in seconds (default: 1800)
EOF
}

case "${1:-}" in
    start)  start_daemon ;;
    stop)   stop_daemon ;;
    status) show_status ;;
    once)   do_refresh ;;
    loop)   run_loop ;;
    *)      show_usage; exit 1 ;;
esac
