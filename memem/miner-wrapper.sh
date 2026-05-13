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
DAEMON_CMD="${MEMEM_DAEMON_CMD:-python3 -m memem.miner_daemon}"
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

_wait_for_dead() {
    # _wait_for_dead PID TIMEOUT_SECS
    # Returns 0 if process dies within TIMEOUT_SECS, 1 if still alive.
    local pid="$1"
    local timeout="$2"
    local elapsed=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$elapsed" -ge "$timeout" ]; then
            return 1
        fi
        sleep 0.5
        elapsed=$(( elapsed + 1 ))
    done
    return 0
}

_kill_with_escalation() {
    # _kill_with_escalation PID LABEL
    # Sends SIGTERM, waits up to 5s, escalates to SIGKILL if needed.
    # Returns 0 if process is dead, 1 if still alive after SIGKILL.
    local pid="$1"
    local label="$2"
    if ! kill -0 "$pid" 2>/dev/null; then
        return 0  # already dead
    fi
    kill -TERM "$pid" 2>/dev/null
    if _wait_for_dead "$pid" 10; then
        echo "$label stopped (PID $pid)"
        return 0
    fi
    echo "$label did not stop after SIGTERM — escalating to SIGKILL (PID $pid)"
    kill -KILL "$pid" 2>/dev/null
    if _wait_for_dead "$pid" 4; then
        echo "$label killed (PID $pid)"
        return 0
    fi
    echo "ERROR: $label still alive after SIGKILL (PID $pid)" >&2
    return 1
}

stop_wrapper() {
    local exit_code=0

    # --- Stop the daemon process ---
    # Read daemon PID from miner.pid (written by miner_daemon.py)
    DAEMON_PID_FILE="$MEMEM_DIR/miner.pid"
    if [ -f "$DAEMON_PID_FILE" ]; then
        DAEMON_PID=$(cat "$DAEMON_PID_FILE" 2>/dev/null)
        if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
            _kill_with_escalation "$DAEMON_PID" "Miner daemon" || exit_code=1
        fi
        rm -f "$DAEMON_PID_FILE"
    else
        # Fallback: use the Python daemon's own stop command to clean up its
        # internal state (PID file, lock file release).
        $DAEMON_CMD stop 2>/dev/null
    fi

    # Clean up the global lock file (stale after daemon death)
    GLOBAL_LOCK_FILE="${HOME}/.memem/miner.global.lock"
    rm -f "$GLOBAL_LOCK_FILE"

    # --- Stop wrapper (loop) process ---
    if [ -f "$WRAPPER_PID_FILE" ]; then
        WRAPPER_PID=$(cat "$WRAPPER_PID_FILE" 2>/dev/null)
        if [ -n "$WRAPPER_PID" ] && kill -0 "$WRAPPER_PID" 2>/dev/null; then
            _kill_with_escalation "$WRAPPER_PID" "Miner wrapper" || exit_code=1
        fi
        rm -f "$WRAPPER_PID_FILE"
    else
        echo "Miner wrapper not running"
    fi

    return $exit_code
}

run_loop() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') INFO Wrapper started — will keep miner alive"

    local backoff="${MEMEM_BACKOFF_INITIAL:-10}"
    local backoff_max=300
    local backoff_reset_threshold=60     # successful run >= 60s resets backoff
    local crash_window_seconds=60
    local crash_window_max=5
    local -a crash_timestamps=()

    while true; do
        echo "$(date '+%Y-%m-%d %H:%M:%S') INFO Wrapper: starting miner (foreground in wrapper)..."

        local run_start
        run_start=$(date +%s)

        # Run miner in foreground — wrapper manages the lifecycle
        $DAEMON_CMD run 2>> "$LOG_FILE"
        EXIT_CODE=$?

        local run_end
        run_end=$(date +%s)
        local run_duration=$(( run_end - run_start ))

        if [ "$EXIT_CODE" -eq 0 ] || [ "$EXIT_CODE" -eq 75 ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') WARN Wrapper: miner exited permanently (code $EXIT_CODE), not restarting."
            rm -f "$WRAPPER_PID_FILE"
            break
        fi

        # Crash detected — record timestamp and check rate
        local now
        now=$(date +%s)
        crash_timestamps+=("$now")
        # Prune timestamps older than the window
        local pruned=()
        for ts in "${crash_timestamps[@]}"; do
            if [ $(( now - ts )) -lt "$crash_window_seconds" ]; then
                pruned+=("$ts")
            fi
        done
        crash_timestamps=("${pruned[@]}")

        if [ "${#crash_timestamps[@]}" -ge "$crash_window_max" ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR Wrapper: $crash_window_max crashes within ${crash_window_seconds}s — refusing to restart. Fix the underlying issue and run \`miner-wrapper.sh start\` manually."
            rm -f "$WRAPPER_PID_FILE"
            break
        fi

        # Reset backoff on successful long-running session
        if [ "$run_duration" -ge "$backoff_reset_threshold" ]; then
            backoff=10
        fi

        echo "$(date '+%Y-%m-%d %H:%M:%S') WARN Wrapper: miner exited (code $EXIT_CODE) after ${run_duration}s, restarting in ${backoff}s..."
        sleep "$backoff"

        # Double backoff for next time, capped
        backoff=$(( backoff * 2 ))
        if [ "$backoff" -gt "$backoff_max" ]; then
            backoff=$backoff_max
        fi
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
