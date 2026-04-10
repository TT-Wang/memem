#!/usr/bin/env python3
"""
Cortex Miner Daemon — persistent background process that watches for
completed Claude Code sessions and mines them with Haiku.

Usage:
    python3 miner-daemon.py start    # start daemon
    python3 miner-daemon.py stop     # stop daemon
    python3 miner-daemon.py status   # check if running
    python3 miner-daemon.py run      # run in foreground (for debugging)
"""

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from miner_protocol import FATAL_EXIT_CODE, TRANSIENT_EXIT_CODE
from session_state import MINED_SESSIONS_FILE, SETTLE_SECONDS, find_settled_sessions, load_mined_session_state
from storage import CORTEX_DIR


PID_FILE = CORTEX_DIR / "miner.pid"
LOG_FILE = CORTEX_DIR / "miner.log"
POLL_INTERVAL = 60

_handler = logging.handlers.RotatingFileHandler(
    str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=2,
)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("cortex-miner")


class FatalMinerError(RuntimeError):
    """Raised when storage state is unsafe and the miner must stop."""


class RetryableMinerError(RuntimeError):
    """Raised for transient per-session failures."""


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


def _cleanup(signum=None, frame=None):
    log.info("Miner daemon stopping (signal %s)", signum)
    PID_FILE.unlink(missing_ok=True)
    sys.exit(0)


def start_daemon():
    existing = _read_pid()
    if existing:
        print(f"Miner daemon already running (PID {existing})")
        return

    # First fork — detach from parent
    pid = os.fork()
    if pid > 0:
        time.sleep(0.5)
        child_pid = _read_pid()
        if child_pid:
            print(f"Miner daemon started (PID {child_pid})")
        else:
            print("Failed to start daemon — check ~/.cortex/miner.log")
        return

    os.setsid()

    # Second fork — prevent acquiring a controlling terminal
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect std file descriptors
    devnull_r = open(os.devnull, "r")
    devnull_w = open(os.devnull, "w")
    os.dup2(devnull_r.fileno(), sys.stdin.fileno())
    os.dup2(devnull_w.fileno(), sys.stdout.fileno())
    os.dup2(devnull_w.fileno(), sys.stderr.fileno())

    _write_pid()
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    log.info("Miner daemon started (PID %d)", os.getpid())
    _run_loop()


def stop_daemon():
    pid = _read_pid()
    if not pid:
        print("Miner daemon not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Miner daemon stopped (PID {pid})")
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        print("Miner daemon was not running (stale PID file cleaned)")


def status_daemon():
    pid = _read_pid()
    if pid:
        print(f"Miner daemon running (PID {pid})")
    else:
        print("Miner daemon not running")


def _run_server_command(args: list[str], expect_json: bool = True):
    server_path = str(Path(__file__).parent / "server.py")
    result = subprocess.run(
        [sys.executable, server_path, *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ.copy(),
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if result.returncode != 0:
        detail = stderr or stdout or f"command failed with exit code {result.returncode}"
        if result.returncode == FATAL_EXIT_CODE:
            raise FatalMinerError(detail)
        raise RetryableMinerError(detail)
    if not expect_json or not stdout:
        return stdout
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RetryableMinerError(
            f"invalid JSON from server.py {' '.join(args)}: {exc}"
        ) from exc


def _mine_session(jsonl_path: Path) -> tuple[int, bool]:
    log.info("Mining session: %s", jsonl_path.stem[:12])

    try:
        result = _run_server_command(["--mine-session", str(jsonl_path)])
        if result.get("skipped"):
            log.info("  -> skipped (%s)", result.get("reason", "unknown"))
            return 0, False
        saved = result.get("memories_saved", 0)
        if saved > 0:
            log.info("  -> %d memories extracted", saved)
        else:
            log.info("  -> no new memories found")
        return saved, True
    except RetryableMinerError as exc:
        log.error("  -> retryable mining failure: %s", exc)
        return 0, False


def _run_loop():
    log.info(
        "Starting mining loop (poll=%ds, settle=%ds, state=%s)",
        POLL_INTERVAL,
        SETTLE_SECONDS,
        MINED_SESSIONS_FILE,
    )

    while True:
        try:
            states = load_mined_session_state()
            sessions = find_settled_sessions(states)
            total_saved = 0
            processed = 0
            for jsonl_path in sessions:
                saved, completed = _mine_session(jsonl_path)
                total_saved += saved
                if completed:
                    processed += 1
            if processed > 0:
                log.info("Rebuilding index after %d completed sessions", processed)
                try:
                    _run_server_command(["--rebuild-index"], expect_json=False)
                except RetryableMinerError as exc:
                    log.error("Index rebuild failed: %s", exc)
        except FatalMinerError as exc:
            log.error("Stopping miner after fatal storage error: %s", exc)
            raise SystemExit(FATAL_EXIT_CODE)
        except Exception as exc:
            log.error("Loop error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "start":
        start_daemon()
    elif cmd == "stop":
        stop_daemon()
    elif cmd == "status":
        status_daemon()
    elif cmd == "run":
        _write_pid()
        signal.signal(signal.SIGTERM, _cleanup)
        signal.signal(signal.SIGINT, _cleanup)
        print(f"Miner running in foreground (PID {os.getpid()})")
        log.info("Miner running in foreground (PID %d)", os.getpid())
        _run_loop()
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run")
        raise SystemExit(TRANSIENT_EXIT_CODE)
