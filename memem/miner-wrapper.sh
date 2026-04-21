#!/usr/bin/env bash
# Cortex Miner Wrapper — keeps the miner daemon alive.
# Auto-restarts on crash. Run this instead of miner_daemon.py directly.
#
# Usage:
#   bash miner-wrapper.sh start   # start with auto-restart
#   bash miner-wrapper.sh stop    # stop daemon + wrapper

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
DAEMON_CMD="python3 -m memem.miner_daemon"
# Prefer MEMEM_DIR, fall back to legacy CORTEX_DIR if a user's shell still exports it.
MEMEM_DIR="${MEMEM_DIR:-${CORTEX_DIR:-$HOME/.memem}}"
WRAPPER_PID_FILE="$MEMEM_DIR/miner-wrapper.pid"
LOG_FILE="$MEMEM_DIR/miner.log"

mkdir -p "$MEMEM_DIR"

is_ephemeral_test_state_dir() {
    if [ -n "${MEMEM_ALLOW_TEST_MINER:-}" ]; then
        return 1
    fi
    case "$MEMEM_DIR" in
        /tmp/pytest-*|/tmp/pytest-of-*|*/pytest-*|*/pytest-of-*) return 0 ;;
    esac
    [ -n "${PYTEST_CURRENT_TEST:-}" ]
}

start_wrapper() {
    if is_ephemeral_test_state_dir; then
        echo "Refusing to start miner wrapper from ephemeral test state: $MEMEM_DIR"
        return 0
    fi

    # Check if wrapper already running
    if [ -f "$WRAPPER_PID_FILE" ]; then
        OLD_PID=$(cat "$WRAPPER_PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Miner wrapper already running (PID $OLD_PID)"
            return
        fi
        rm -f "$WRAPPER_PID_FILE"
    fi

    # Fork to background
    nohup bash "$0" _loop >> "$LOG_FILE" 2>&1 &
    WRAPPER_PID=$!
    echo "$WRAPPER_PID" > "$WRAPPER_PID_FILE"
    echo "Miner wrapper started (PID $WRAPPER_PID)"
}

stop_wrapper() {
    # Stop daemon first
    $DAEMON_CMD stop 2>/dev/null

    # Stop wrapper
    if [ -f "$WRAPPER_PID_FILE" ]; then
        WRAPPER_PID=$(cat "$WRAPPER_PID_FILE")
        if kill -0 "$WRAPPER_PID" 2>/dev/null; then
            kill "$WRAPPER_PID" 2>/dev/null
            echo "Miner wrapper stopped (PID $WRAPPER_PID)"
        fi
        rm -f "$WRAPPER_PID_FILE"
    else
        echo "Miner wrapper not running"
    fi
}

run_loop() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') INFO Wrapper started — will keep miner alive"

    while true; do
        echo "$(date '+%Y-%m-%d %H:%M:%S') INFO Wrapper: starting miner (foreground in wrapper)..."
        # Run miner in foreground — wrapper manages the lifecycle
        $DAEMON_CMD run 2>> "$LOG_FILE"
        EXIT_CODE=$?
        if [ "$EXIT_CODE" -eq 0 ] || [ "$EXIT_CODE" -eq 75 ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') WARN Wrapper: miner exited permanently (code $EXIT_CODE), not restarting."
            rm -f "$WRAPPER_PID_FILE"
            break
        fi
        echo "$(date '+%Y-%m-%d %H:%M:%S') WARN Wrapper: miner exited (code $EXIT_CODE), restarting in 10s..."
        sleep 10
    done
}

case "${1:-status}" in
    start)
        start_wrapper
        ;;
    stop)
        stop_wrapper
        ;;
    status)
        if [ -f "$WRAPPER_PID_FILE" ] && kill -0 "$(cat "$WRAPPER_PID_FILE")" 2>/dev/null; then
            echo "Miner wrapper running (PID $(cat "$WRAPPER_PID_FILE"))"
        else
            echo "Miner wrapper not running"
        fi
        $DAEMON_CMD status
        ;;
    _loop)
        run_loop
        ;;
    *)
        echo "Usage: $0 start|stop|status"
        ;;
esac
