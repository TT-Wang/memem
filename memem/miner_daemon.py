#!/usr/bin/env python3
"""
Cortex Miner Daemon — persistent background process that watches for
completed Claude Code sessions and mines them with Haiku.

Usage:
    python3 miner_daemon.py start    # start daemon
    python3 miner_daemon.py stop     # stop daemon
    python3 miner_daemon.py status   # check if running
    python3 miner_daemon.py run      # run in foreground (for debugging)
"""

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from memem.miner_protocol import FATAL_EXIT_CODE, TRANSIENT_EXIT_CODE
from memem.models import MEMEM_DIR
from memem.session_state import (
    MINED_SESSIONS_FILE,
    SETTLE_SECONDS,
    _ensure_installed_at,
    find_settled_sessions,
    load_mined_session_state,
)

PID_FILE = MEMEM_DIR / "miner.pid"
LOG_FILE = MEMEM_DIR / "miner.log"
POLL_INTERVAL = 60
GLOBAL_LOCK_FILE = Path.home() / ".memem" / "miner.global.lock"
_GLOBAL_LOCK_FH = None

log = logging.getLogger("memem-miner")


def _configure_logging() -> None:
    """Attach the rotating file handler — called only when the daemon actually starts.

    Deferring this out of module scope keeps ``import memem.miner_daemon``
    side-effect-free so tests and tooling can import it without creating the
    real ``~/.cortex/miner.log`` file or clobbering the host process root logger.
    """
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        return  # Already configured
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=2,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


class FatalMinerError(RuntimeError):
    """Raised when storage state is unsafe and the miner must stop."""


class RetryableMinerError(RuntimeError):
    """Raised for transient per-session failures."""


def _is_ephemeral_test_state_dir(path: Path = MEMEM_DIR) -> bool:
    """True for pytest temp state dirs that must not start persistent miners."""
    if os.environ.get("MEMEM_ALLOW_TEST_MINER"):
        return False
    path_str = str(path)
    return (
        "/pytest-" in path_str
        or "/pytest-of-" in path_str
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _refuse_ephemeral_test_miner() -> bool:
    if not _is_ephemeral_test_state_dir():
        return False
    message = f"Refusing to start miner from ephemeral test state: {MEMEM_DIR}"
    print(message)
    log.warning(message)
    return True


def _acquire_global_lock() -> bool:
    """Allow only one memem miner per OS user, regardless of MEMEM_DIR."""
    global _GLOBAL_LOCK_FH
    GLOBAL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(GLOBAL_LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _GLOBAL_LOCK_FH = fh
    return True


def _release_global_lock() -> None:
    global _GLOBAL_LOCK_FH
    if _GLOBAL_LOCK_FH is None:
        return
    try:
        _GLOBAL_LOCK_FH.seek(0)
        _GLOBAL_LOCK_FH.truncate()
        fcntl.flock(_GLOBAL_LOCK_FH.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        _GLOBAL_LOCK_FH.close()
    except OSError:
        pass
    _GLOBAL_LOCK_FH = None


def _ensure_single_miner() -> bool:
    if _acquire_global_lock():
        return True
    message = f"Another memem miner is already running (global lock: {GLOBAL_LOCK_FILE})"
    print(message)
    log.warning(message)
    return False


def _is_quota_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "hit your limit" in text or "rate limit" in text or "quota" in text


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
    _release_global_lock()
    sys.exit(0)


def start_daemon():
    _configure_logging()
    if _refuse_ephemeral_test_miner():
        return
    existing = _read_pid()
    if existing:
        print(f"Miner daemon already running (PID {existing})")
        return

    # First fork — detach from parent
    pid = os.fork()
    if pid > 0:
        # Poll for the grandchild's PID file instead of a blind sleep — avoids
        # false "Failed to start" messages on slow/cold-start hosts.
        child_pid = None
        for _ in range(20):  # up to 2s total
            time.sleep(0.1)
            child_pid = _read_pid()
            if child_pid:
                break
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
    devnull_r = open(os.devnull)
    devnull_w = open(os.devnull, "w")
    os.dup2(devnull_r.fileno(), sys.stdin.fileno())
    os.dup2(devnull_w.fileno(), sys.stdout.fileno())
    os.dup2(devnull_w.fileno(), sys.stderr.fileno())

    if not _ensure_single_miner():
        raise SystemExit(0)

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
    plugin_root = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "memem.server", *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
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
        if _is_quota_limit_error(exc):
            raise FatalMinerError(f"Claude quota/rate limit reached: {exc}") from exc
        log.error("  -> retryable mining failure: %s", exc)
        return 0, False


def _run_loop():
    log.info(
        "Starting mining loop (poll=%ds, settle=%ds, state=%s)",
        POLL_INTERVAL,
        SETTLE_SECONDS,
        MINED_SESSIONS_FILE,
    )
    # Establish the install-time gate on first daemon run so subsequent
    # scans only mine sessions created after the daemon started. v0.10.2:
    # _get_installed_at is now read-only, so we have to create the marker
    # explicitly here instead of relying on lazy-creation.
    _ensure_installed_at()

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
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "start":
        start_daemon()
    elif cmd == "stop":
        stop_daemon()
    elif cmd == "status":
        status_daemon()
    elif cmd == "run":
        _configure_logging()
        if _refuse_ephemeral_test_miner():
            raise SystemExit(0)
        if not _ensure_single_miner():
            raise SystemExit(0)
        _write_pid()
        signal.signal(signal.SIGTERM, _cleanup)
        signal.signal(signal.SIGINT, _cleanup)
        print(f"Miner running in foreground (PID {os.getpid()})")
        log.info("Miner running in foreground (PID %d)", os.getpid())
        _run_loop()
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run")
        raise SystemExit(TRANSIENT_EXIT_CODE)
