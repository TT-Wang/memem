#!/usr/bin/env bash
# Cortex Miner Wrapper — keeps the miner daemon alive.
# Auto-restarts on crash. Run this instead of miner-daemon.py directly.
#
# Usage:
#   bash miner-wrapper.sh start   # start with auto-restart
#   bash miner-wrapper.sh stop    # stop daemon + wrapper

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON="$SCRIPT_DIR/miner-daemon.py"
CORTEX_DIR="${CORTEX_DIR:-$HOME/.cortex}"
WRAPPER_PID_FILE="$CORTEX_DIR/miner-wrapper.pid"
LOG_FILE="$CORTEX_DIR/miner.log"

mkdir -p "$CORTEX_DIR"

start_wrapper() {
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
    python3 "$DAEMON" stop 2>/dev/null

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
        python3 "$DAEMON" run 2>> "$LOG_FILE"
        EXIT_CODE=$?
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
        python3 "$DAEMON" status
        ;;
    _loop)
        run_loop
        ;;
    *)
        echo "Usage: $0 start|stop|status"
        ;;
esac
