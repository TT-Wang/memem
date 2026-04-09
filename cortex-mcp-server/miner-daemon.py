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

import os
import sys
import time
import signal
import logging
from pathlib import Path
from datetime import datetime, timezone

# ─── Config ──────────────────────────────────────────────────────

CORTEX_DIR = Path(os.environ.get("CORTEX_DIR", os.path.expanduser("~/.cortex")))
PID_FILE = CORTEX_DIR / "miner.pid"
LOG_FILE = CORTEX_DIR / "miner.log"
SESSIONS_DIR = Path.home() / ".claude" / "projects"

# How long a file must be unchanged before we consider the session "ended"
SETTLE_SECONDS = 300  # 5 minutes of no writes = session done

# How often to check for new sessions
POLL_INTERVAL = 60  # check every 60 seconds

# ─── Logging ─────────────────────────────────────────────────────

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cortex-miner")


# ─── Daemon lifecycle ────────────────────────────────────────────

def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process actually exists
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

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly then confirm
        time.sleep(0.5)
        child_pid = _read_pid()
        if child_pid:
            print(f"Miner daemon started (PID {child_pid})")
        else:
            print("Failed to start daemon — check ~/.cortex/miner.log")
        return

    # Child — detach
    os.setsid()
    # Redirect stdio
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

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


# ─── Mining loop ─────────────────────────────────────────────────

def _find_settled_sessions() -> list[Path]:
    """Find JSONL session files that haven't been modified recently."""
    if not SESSIONS_DIR.exists():
        return []

    now = time.time()
    settled = []

    for jsonl in SESSIONS_DIR.rglob("*.jsonl"):
        if "/subagents/" in str(jsonl):
            continue
        # Skip tiny files
        try:
            stat = jsonl.stat()
            if stat.st_size < 5000:
                continue
            # Check if file has settled (no writes for SETTLE_SECONDS)
            if (now - stat.st_mtime) > SETTLE_SECONDS:
                settled.append(jsonl)
        except OSError:
            continue

    return settled


def _mine_and_index(jsonl_path: Path):
    """Mine a single session and rebuild index."""
    # Import here to avoid loading ChromaDB at daemon start
    sys.path.insert(0, str(Path(__file__).parent))
    from server import mine_session, _generate_index

    log.info("Mining session: %s", jsonl_path.stem[:12])

    try:
        result = mine_session(str(jsonl_path))
        if result.get("skipped"):
            return
        saved = result.get("memories_saved", 0)
        if saved > 0:
            log.info("  → %d memories extracted, rebuilding index", saved)
            _generate_index()
        else:
            log.info("  → no new memories found")
    except Exception as e:
        log.error("  → mining failed: %s", e)


def _run_loop():
    """Main polling loop — find settled sessions, mine them."""
    # Import server module once
    sys.path.insert(0, str(Path(__file__).parent))

    log.info("Starting mining loop (poll=%ds, settle=%ds)", POLL_INTERVAL, SETTLE_SECONDS)

    while True:
        try:
            sessions = _find_settled_sessions()
            for jsonl in sessions:
                _mine_and_index(jsonl)
        except Exception as e:
            log.error("Loop error: %s", e)

        time.sleep(POLL_INTERVAL)


# ─── CLI ─────────────────────────────────────────────────────────

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
        # Foreground mode for debugging
        _write_pid()
        signal.signal(signal.SIGTERM, _cleanup)
        signal.signal(signal.SIGINT, _cleanup)
        print(f"Miner running in foreground (PID {os.getpid()})")
        log.info("Miner running in foreground (PID %d)", os.getpid())
        _run_loop()
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run")
