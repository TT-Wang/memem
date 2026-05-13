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
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import structlog
from tenacity import (
    wait_random_exponential,  # noqa: F401 — imported for dep resolution; formula implemented manually below
)

from memem.miner_circuit_breaker import CircuitBreaker
from memem.miner_errors import PermanentError, TransientError
from memem.miner_protocol import (
    FATAL_EXIT_CODE,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RETRYING,
    TRANSIENT_EXIT_CODE,
)
from memem.models import MEMEM_DIR
from memem.reaper import reap_orphan_haiku_procs
from memem.session_state import (
    HARD_RETRY_CAP,
    MINED_SESSIONS_FILE,
    SETTLE_SECONDS,
    _ensure_installed_at,
    find_settled_sessions,
    load_mined_session_state,
    update_session_state,
)

PID_FILE = MEMEM_DIR / "miner.pid"
LOG_FILE = MEMEM_DIR / "miner.log"
HEARTBEAT_FILE = MEMEM_DIR / "miner.heartbeat"
_shutdown_requested = False
POLL_INTERVAL = 60
MAX_CONCURRENT_SUBPROCESSES = 1
_subprocess_semaphore = threading.Semaphore(MAX_CONCURRENT_SUBPROCESSES)
SUBPROCESS_TIMEOUT_SECONDS = 300  # was hardcoded as timeout=300 in subprocess.run
SUBPROCESS_KILL_GRACE_SECONDS = 5  # SIGTERM grace before SIGKILL
# After this many consecutive failures on the same session, persist STATUS_FAILED
# so it stops being re-attempted every poll. The fingerprint check in
# find_settled_sessions will let it back in if the JSONL file changes.
MAX_SESSION_FAILURES = 3
# When every session in a poll fails, ramp the next sleep up to this cap to
# prevent a logged-out claude CLI from spawning subprocesses every 60 seconds.
BACKOFF_MAX_SECONDS = 1800

assert HARD_RETRY_CAP >= MAX_SESSION_FAILURES, (
    f"misconfiguration: HARD_RETRY_CAP={HARD_RETRY_CAP} < MAX_SESSION_FAILURES={MAX_SESSION_FAILURES} — "
    f"sessions will be locked before the daemon's per-run failure counter trips"
)
GLOBAL_LOCK_FILE = Path.home() / ".memem" / "miner.global.lock"
_GLOBAL_LOCK_FH = None

log = structlog.get_logger("memem-miner")

_circuit_breaker = CircuitBreaker()


def _write_heartbeat() -> None:
    """Touch the heartbeat file with the current timestamp.

    Used by --status to detect a hung daemon: if heartbeat mtime is older
    than 2x POLL_INTERVAL, something is wrong.
    """
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError as exc:
        log.warning("heartbeat_write_failed", error=str(exc))


def _next_backoff_seconds(attempt: int) -> float:
    """Full Jitter backoff (AWS de-correlation pattern) — random in [0, 2^attempt) capped.

    attempt=1 -> random in [0, 2)
    attempt=2 -> random in [0, 4)
    attempt=3 -> random in [0, 8)
    ... capped at BACKOFF_MAX_SECONDS (1800s)

    Full Jitter spreads simultaneous restarts that would otherwise pile up
    at the same moment after a shared failure (the "thundering herd" problem).
    tenacity is imported above as a runtime dep; the formula is implemented
    manually because tenacity's wait API is decorator-oriented.
    """
    cap = min(2 ** attempt, BACKOFF_MAX_SECONDS)
    return random.uniform(0, cap)


def _configure_logging() -> None:
    """Configure structlog for JSON output to a rotating file handler.

    Deferring this out of module scope keeps ``import memem.miner_daemon``
    side-effect-free so tests and tooling can import it without creating the
    real ``~/.memem/miner.log`` file or clobbering the host process root logger.
    """
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger("memem-miner").handlers):
        return
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    # v1.8.1: 0600 perms — log contains session IDs which correlate to private slice records.
    old_umask = os.umask(0o177)
    try:
        handler = logging.handlers.RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
    finally:
        os.umask(old_umask)
    handler.setFormatter(logging.Formatter("%(message)s"))  # structlog renders the JSON itself
    stdlib_logger = logging.getLogger("memem-miner")
    stdlib_logger.addHandler(handler)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# Backward-compat aliases — new code should use the taxonomy classes directly.
RetryableMinerError = TransientError
FatalMinerError = PermanentError


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
    log.warning("ephemeral_test_miner_refused", memem_dir=str(MEMEM_DIR))
    return True


def _try_acquire_lock_once() -> bool | None:
    """Attempt one global lock acquisition cycle.

    Returns:
        True  — lock acquired successfully
        False — lock held by a live process (genuine conflict)
        None  — stale lock detected and removed; caller should retry
    """
    global _GLOBAL_LOCK_FH
    GLOBAL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o177)  # 0o177 = restrict to owner-only (0600)
    try:
        fh = open(GLOBAL_LOCK_FILE, "a+")
    finally:
        os.umask(old_umask)
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Lock is held — check if the PID in the file is still alive
        try:
            fh.seek(0)
            content = fh.read().strip()
            pid = int(content.splitlines()[0]) if content else 0
        except (ValueError, IndexError, OSError):
            pid = 0
        fh.close()
        if pid > 0:
            try:
                os.kill(pid, 0)  # raises ProcessLookupError if dead, PermissionError if foreign-owned
                # Process is alive — genuine conflict
                return False
            except ProcessLookupError:
                # Process is dead — stale lock file, remove and signal retry
                log.warning(
                    "stale_global_lock_removed",
                    stale_pid=pid,
                    lock_file=str(GLOBAL_LOCK_FILE),
                )
                try:
                    GLOBAL_LOCK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                return None  # signal: retry
            except PermissionError:
                # Process is alive but owned by another user — genuine conflict
                # (v1.8.1: previously caught as OSError and miscategorized as dead,
                # which would let a second daemon start on multi-user systems).
                return False
        # pid=0 means unreadable content — treat as stale
        try:
            GLOBAL_LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return None  # signal: retry
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _GLOBAL_LOCK_FH = fh
    return True


def _acquire_global_lock() -> bool:
    """Allow only one memem miner per OS user, regardless of MEMEM_DIR.

    If the lock file contains a stale PID (process no longer alive), removes
    the lock file and retries acquisition once. If a live process holds the
    lock, returns False immediately (no retry).
    """
    result = _try_acquire_lock_once()
    if result is None:
        # Stale lock was cleaned up — retry once
        result = _try_acquire_lock_once()
        if result is None:
            # If still None after retry (very unlikely race), treat as conflict
            return False
    return bool(result)


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
    log.warning("miner_already_running", global_lock=str(GLOBAL_LOCK_FILE))
    return False


def _is_fatal_api_error(exc: BaseException) -> bool:
    """Errors that won't recover by retrying — stop the miner instead of looping.

    Auth/login failures used to be classified as retryable, which produced
    thousands of subprocess spawns per hour when the `claude` CLI was logged
    out. Now we surface them as fatal so the daemon exits cleanly and waits
    for the user to re-authenticate.

    Subprocess timeouts (subprocess.TimeoutExpired or a RetryableMinerError
    wrapping a timeout message) are TRANSIENT — the session may be huge and
    Haiku struggled, but the miner can continue mining other sessions. They
    must NOT be classified as fatal (that would stop the whole miner daemon).
    Per-session timeout accumulation is handled separately by mine_session via
    the MEMEM_MAX_SESSION_TIMEOUTS cap.

    HTTP 401/403 / auth key errors ARE fatal: no amount of retrying will fix
    a broken API key. The miner should stop and let the user re-authenticate.
    """
    # subprocess.TimeoutExpired → transient, never fatal
    if isinstance(exc, subprocess.TimeoutExpired):
        return False

    text = str(exc).lower()

    # Timeout patterns from _run_server_command's RetryableMinerError wrapping
    # (e.g. "subprocess timed out after 300s; killed process group"). These are
    # transient — the miner moves on to the next session.
    transient_timeout_patterns = (
        "subprocess timed out after",
        "timed out after",
    )
    if any(pattern in text for pattern in transient_timeout_patterns):
        return False

    fatal_patterns = (
        "hit your limit",
        "rate limit",
        "quota",
        "authentication_error",
        "invalid authentication",
        "invalid_api_key",
        "authentication failed",
        "could not load credentials",
        "you are not logged in",
        "please run /login",
    )
    return any(pattern in text for pattern in fatal_patterns)


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


def _request_shutdown(signum, frame):
    """Set shutdown flag; main loop drains and exits cleanly at next iteration."""
    global _shutdown_requested
    _shutdown_requested = True
    log.info("shutdown_requested", signal=signum)


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
            print(f"Failed to start daemon — check {LOG_FILE}")
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
    devnull_r.close()
    devnull_w.close()

    if not _ensure_single_miner():
        raise SystemExit(0)

    _write_pid()
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    log.info("miner_daemon_started", pid=os.getpid())
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
    from memem.status import render_status  # noqa: PLC0415
    print(render_status())


def _run_server_command(args: list[str], expect_json: bool = True):
    plugin_root = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    env["PYTHONPATH"] = plugin_root + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "memem.server", *args]
    with _subprocess_semaphore, subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as p:
        try:
            stdout, stderr = p.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # kill the whole process group, not just the child
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass  # already dead
            try:
                stdout, stderr = p.communicate(timeout=SUBPROCESS_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = p.communicate()
            raise RetryableMinerError(
                f"subprocess timed out after {SUBPROCESS_TIMEOUT_SECONDS}s; killed process group"
            )

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if p.returncode != 0:
        detail = stderr or stdout or f"command failed with exit code {p.returncode}"
        if p.returncode == TRANSIENT_EXIT_CODE:
            raise TransientError(detail)
        # Default: anything else (including FATAL_EXIT_CODE and unknown codes) is permanent.
        raise PermanentError(detail)
    if not expect_json or not stdout:
        return stdout
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RetryableMinerError(
            f"invalid JSON from server.py {' '.join(args)}: {exc}"
        ) from exc


def _mine_session(jsonl_path: Path) -> tuple[int, bool]:
    sid = jsonl_path.stem[:12]
    log.info("mining_session", session_id=sid)
    t0 = time.monotonic()

    try:
        result = _run_server_command(["--mine-session", str(jsonl_path)])
        duration_ms = int((time.monotonic() - t0) * 1000)
        if result.get("skipped"):
            log.info("session_processed", session_id=sid, outcome="skipped", duration_ms=duration_ms, reason=result.get("reason", "unknown"))
            return 0, False
        saved = result.get("memories_saved", 0)
        outcome = "success" if saved > 0 else "empty"
        log.info("session_processed", session_id=sid, outcome=outcome, duration_ms=duration_ms, memories_saved=saved)
        return saved, True
    except RetryableMinerError as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        if _is_fatal_api_error(exc):
            # Persist STATUS_FAILED for this session BEFORE the daemon exits.
            # Without this, the next wrapper restart re-picks the same session,
            # hits the same hang/auth error, and crashes again — burning the
            # 5-in-60s wrapper crash budget for no progress. (Recurring crash
            # on session 9612f54c-bbd across Apr 30 / May 1 / May 4 was the
            # symptom that surfaced this gap.)
            try:
                update_session_state(
                    jsonl_path,
                    STATUS_FAILED,
                    message=f"fatal API/auth/timeout: {str(exc)[:200]}",
                    attempts=MAX_SESSION_FAILURES,
                )
            except OSError as persist_exc:
                log.error("persist_fatal_state_failed", session_id=sid, error=str(persist_exc))
            raise FatalMinerError(f"Claude API/auth error, miner stopping: {exc}") from exc
        log.error("session_processed", session_id=sid, outcome="failure", duration_ms=duration_ms, error=str(exc))
        return 0, False


def has_retryable_sessions(states: dict) -> bool:
    """Return True if any session can still make forward progress.

    A session is "retryable" when it is not yet terminal AND it has not
    exhausted the hard retry cap.  Terminal means STATUS_COMPLETE or
    HARD_RETRY_CAP attempts reached.

    Used by ``compute_sleep_cap`` to decide whether the daemon should
    back off aggressively (no retryable sessions → allow up to
    BACKOFF_MAX_SECONDS) or stay responsive (cap at half the max).
    """
    from memem.session_state import HARD_RETRY_CAP  # noqa: PLC0415

    for state in states.values():
        status = state.get("status", "")
        attempts = int(state.get("attempts", 0) or 0)
        if status != STATUS_COMPLETE and attempts < HARD_RETRY_CAP:
            return True
    return False


def compute_sleep_cap(sleep_seconds: int, states: dict) -> int:
    """Return the sleep duration capped by the retryable-session heuristic.

    If any session is retryable (``has_retryable_sessions`` returns True),
    the cap is ``BACKOFF_MAX_SECONDS // 2`` (900s) so the daemon checks
    back sooner.  When all sessions are terminal, allow the full
    ``BACKOFF_MAX_SECONDS`` (1800s) because there is nothing useful to do
    sooner.

    This is a pure function — no I/O, no side-effects — so it can be
    unit-tested without spinning up the real daemon.
    """
    if has_retryable_sessions(states):
        return min(sleep_seconds, BACKOFF_MAX_SECONDS // 2)
    return min(sleep_seconds, BACKOFF_MAX_SECONDS)


def _seed_failure_counts_from_state(states: dict) -> dict[str, int]:
    """Seed the in-memory failure counter from persisted session state.

    On daemon restart the in-memory failure_counts dict is empty, so
    sessions with persisted attempts=N would reset to 0 and could
    ping-pong between 0 and MAX-1 across restarts forever. This helper
    reads the persisted attempts value for each non-complete session and
    returns an initial counter so the cap enforcement picks up where the
    previous run left off.

    COMPLETE sessions are excluded (they succeeded and should not count
    against the failure cap). Sessions with attempts=0 are excluded (no
    failures recorded yet, nothing to seed).
    """
    excluded = {STATUS_COMPLETE, STATUS_BLOCKED}
    return {
        sid: int(s.get("attempts", 0))
        for sid, s in states.items()
        if int(s.get("attempts", 0)) > 0 and s.get("status") not in excluded
    }


STUCK_CLEANUP_HOURS = int(os.environ.get("MEMEM_STUCK_CLEANUP_HOURS", "2"))

# TTL in days before a pending procedural suggestion is auto-archived as expired.
MEMEM_PROCEDURAL_TTL_DAYS = int(os.environ.get("MEMEM_PROCEDURAL_TTL_DAYS", "7"))


def _cleanup_stuck_sessions(db_path: "Path") -> int:
    """Reset STATUS_IN_PROGRESS sessions older than STUCK_CLEANUP_HOURS to STATUS_FAILED.

    On daemon restart, sessions left in STATUS_IN_PROGRESS indicate the process
    died mid-mine (SIGKILL, OOM, power loss). They will never self-heal unless
    explicitly reset. This sweep runs once at startup and moves them back to
    STATUS_FAILED so they re-enter the normal retry queue (subject to HARD_RETRY_CAP).

    Returns the number of sessions reset.
    """
    cutoff_dt = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - STUCK_CLEANUP_HOURS * 3600),
    )
    # Import here to avoid circular imports at module load time.
    from memem import session_state_db  # noqa: PLC0415

    try:
        with session_state_db._connect(db_path) as conn:
            # Count candidates first for the log line
            rows = conn.execute(
                "SELECT COUNT(*) FROM mined_sessions WHERE status = 'in_progress' AND updated_at < ?",
                (cutoff_dt,),
            ).fetchone()
            count = rows[0] if rows else 0
            if count > 0:
                conn.execute(
                    """
                    UPDATE mined_sessions
                       SET status = 'failed',
                           last_error = 'abandoned in-progress (process died mid-mine)',
                           message = 'abandoned in-progress (process died mid-mine)',
                           attempts = attempts + 1,
                           updated_at = ?
                     WHERE status = 'in_progress'
                       AND updated_at < ?
                    """,
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cutoff_dt),
                )
    except (sqlite3.Error, OSError) as exc:
        log.error("stuck_cleanup_failed", error=str(exc))
        return 0
    return count


def _archive_expired_procedural_suggestions() -> int:
    """Mark procedural-suggestion memories older than MEMEM_PROCEDURAL_TTL_DAYS as 'expired'.

    Finds all memories with tag ``kind:procedural-suggestion`` AND
    ``status: pending_review`` AND ``created_iso`` older than the TTL.
    Updates each to ``status: expired`` to keep the audit trail without
    cluttering the SessionStart briefing.

    Returns the number of suggestions archived.
    """
    try:
        from datetime import UTC, datetime, timedelta

        from memem.models import now_iso, parse_iso_dt
        from memem.obsidian_store import (
            _cache_refresh_from_disk,
            _find_memory,
            _obsidian_memories,
            _write_obsidian_memory,
        )
    except Exception as exc:
        log.warning("procedural_archive_import_failed", error=str(exc))
        return 0

    try:
        all_mems = _obsidian_memories()
    except Exception as exc:
        log.warning("procedural_archive_load_failed", error=str(exc))
        return 0

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=MEMEM_PROCEDURAL_TTL_DAYS)
    archived = 0

    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:procedural-suggestion" not in tags:
            continue
        if mem.get("status") != "pending_review":
            continue
        # Parse created_iso or fall back to created_at
        ts_str = str(mem.get("created_iso") or mem.get("created_at") or "")
        if not ts_str:
            continue
        created_dt = parse_iso_dt(ts_str)
        if created_dt is None:
            continue
        if created_dt >= cutoff:
            continue  # still within TTL
        # Archive it: update status to expired
        mem_id = mem.get("id", "")
        try:
            fresh = _find_memory(mem_id)
            if not fresh:
                continue
            fresh["status"] = "expired"
            fresh["updated_at"] = now_iso()
            _write_obsidian_memory(fresh)
            _cache_refresh_from_disk(mem_id)
            archived += 1
        except Exception as exc:
            log.warning("procedural_archive_update_failed", memory_id=str(mem_id)[:8], error=str(exc))

    return archived


def _run_loop():
    log.info(
        "mining_loop_started",
        poll_interval=POLL_INTERVAL,
        settle_seconds=SETTLE_SECONDS,
        state_file=str(MINED_SESSIONS_FILE),
    )
    # Establish the install-time gate on first daemon run so subsequent
    # scans only mine sessions created after the daemon started. v0.10.2:
    # _get_installed_at is now read-only, so we have to create the marker
    # explicitly here instead of relying on lazy-creation.
    _ensure_installed_at()

    # Startup stuck-session sweep: sessions left in STATUS_IN_PROGRESS from a
    # previous daemon crash are reset to STATUS_FAILED so they re-enter the
    # retry queue. Without this, they stay stuck forever until the file changes.
    from memem.session_state import _db_path as _get_db_path  # noqa: PLC0415
    db_path = _get_db_path()
    stuck_count = _cleanup_stuck_sessions(db_path)
    log.info("stuck_cleanup", reset_count=stuck_count, cutoff_hours=STUCK_CLEANUP_HOURS)
    print(f"[miner] stuck-cleanup: reset {stuck_count} sessions from STATUS_IN_PROGRESS")

    # Startup procedural-suggestion sweep: archive suggestions older than TTL.
    expired_count = _archive_expired_procedural_suggestions()
    log.info("procedural_suggestion_archive", archived=expired_count, ttl_days=MEMEM_PROCEDURAL_TTL_DAYS)
    print(f"[miner] procedural-suggestion: archived {expired_count} expired suggestions (>{MEMEM_PROCEDURAL_TTL_DAYS} days)")

    # Seed failure_counts from persisted state so restarts don't reset the
    # counter. Without this, a flaky session could ping-pong forever between
    # failures=0 and failures=MAX-1 across daemon restarts.
    failure_counts: dict[str, int] = _seed_failure_counts_from_state(load_mined_session_state())
    sleep_seconds = POLL_INTERVAL
    backoff_attempt = 0

    while True:
        _write_heartbeat()
        reap_count = reap_orphan_haiku_procs()
        if reap_count > 0:
            log.info("orphan_reaper_sweep", reaped_count=reap_count)
        if _shutdown_requested:
            log.info("shutdown_draining")
            _release_global_lock()
            try:
                PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return
        try:
            states = load_mined_session_state()
            sessions = find_settled_sessions(states)
            total_saved = 0
            processed = 0
            attempted = 0
            failed = 0
            for jsonl_path in sessions:
                # Inner-loop heartbeat: each _mine_session can take ~150s; with thousands of
                # queued sessions the outer-loop heartbeat alone goes stale for hours and
                # --status falsely reports the daemon dead. Writing here keeps freshness
                # bounded by per-session duration regardless of queue depth (v1.7.2 m3).
                _write_heartbeat()
                if _circuit_breaker.is_open():
                    # Skip processing; mark blocked so it stays out of the queue
                    # until next file change.
                    try:
                        update_session_state(jsonl_path, STATUS_BLOCKED, message="circuit breaker open")
                    except OSError as exc:
                        log.error("status_blocked_write_failed", error=str(exc))
                    continue

                attempted += 1
                try:
                    saved, completed = _mine_session(jsonl_path)
                    total_saved += saved
                    if completed:
                        _circuit_breaker.record_success()
                        processed += 1
                        failure_counts.pop(jsonl_path.stem, None)
                    else:
                        count = failure_counts.get(jsonl_path.stem, 0) + 1
                        failure_counts[jsonl_path.stem] = count
                        failed += 1
                        if count >= MAX_SESSION_FAILURES:
                            log.warning(
                                "session_marked_failed",
                                session_id=jsonl_path.stem[:12],
                                attempts=count,
                            )
                            try:
                                update_session_state(
                                    jsonl_path,
                                    STATUS_FAILED,
                                    message=f"miner gave up after {count} consecutive failures",
                                    attempts=count,
                                )
                            except OSError as exc:
                                log.error("persist_failure_state_failed", error=str(exc))
                            failure_counts.pop(jsonl_path.stem, None)
                        else:
                            try:
                                update_session_state(
                                    jsonl_path,
                                    STATUS_RETRYING,
                                    message=f"transient failure, attempt {count}",
                                    attempts=count,
                                )
                            except OSError as exc:
                                log.error("persist_retrying_state_failed", error=str(exc))
                except PermanentError as exc:
                    _circuit_breaker.record_failure(exc)
                    # Persist STATUS_FAILED before re-raising so a wrapper
                    # restart sees the session as terminal and skips it. Without
                    # this, the same session triggers PermanentError again on
                    # restart, repeating until the wrapper's 5-in-60s crash
                    # guard fires (5 rapid wrapper restarts before stop).
                    try:
                        update_session_state(
                            jsonl_path,
                            STATUS_FAILED,
                            message=f"permanent error: {exc}",
                            attempts=failure_counts.get(jsonl_path.stem, 0) + 1,
                        )
                    except OSError as persist_exc:
                        log.error("persist_failed_state_failed", error=str(persist_exc))
                    raise  # let outer handler do its thing
            if processed > 0:
                log.info("rebuilding_index", completed_sessions=processed)
                try:
                    _run_server_command(["--rebuild-index"], expect_json=False)
                except RetryableMinerError as exc:
                    log.error("index_rebuild_failed", error=str(exc))

            # Backoff: if every attempt failed, apply Full Jitter backoff up to the cap.
            # On any progress, snap back to the normal poll interval.
            if attempted > 0 and failed == attempted:
                backoff_attempt += 1
                sleep_seconds = max(POLL_INTERVAL, _next_backoff_seconds(backoff_attempt))
                log.info(
                    "all_sessions_failed_backoff",
                    attempted=attempted,
                    sleep_seconds=round(sleep_seconds, 1),
                    backoff_attempt=backoff_attempt,
                )
            else:
                backoff_attempt = 0
                sleep_seconds = POLL_INTERVAL
        except FatalMinerError as exc:
            log.error("fatal_error_stopping_miner", error=str(exc))
            raise SystemExit(FATAL_EXIT_CODE)
        except Exception as exc:
            log.error("loop_error", error=str(exc))

        # Tighten backoff when retryable sessions are present so the daemon
        # checks back sooner rather than sleeping for the full 1800s cap.
        # Uses a fresh state snapshot so newly-added sessions are reflected.
        try:
            _current_states = load_mined_session_state()
            sleep_seconds = compute_sleep_cap(sleep_seconds, _current_states)
        except Exception:
            pass  # non-fatal — worst case we sleep a bit longer

        time.sleep(sleep_seconds)


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
        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)
        print(f"Miner running in foreground (PID {os.getpid()})")
        log.info("miner_foreground_started", pid=os.getpid())
        _run_loop()
    else:
        print(f"Usage: {sys.argv[0]} start|stop|status|run")
        raise SystemExit(TRANSIENT_EXIT_CODE)
