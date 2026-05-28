"""Slice Daemon — long-running process that keeps the embedding model warm.

Instead of spawning a fresh ``python3 -m memem.server slice ...`` subprocess
(which cold-loads ~500 MB of sentence-transformers) on every UserPromptSubmit
hook fire, this daemon holds the model in-process and serves slice requests
over a Unix socket in ~50-500 ms.

Usage (CLI):
    python3 -m memem.slice_daemon          # alias for 'start'
    python3 -m memem.slice_daemon start    # start daemon (background)
    python3 -m memem.slice_daemon stop     # SIGTERM existing daemon
    python3 -m memem.slice_daemon status   # print running/stopped
    python3 -m memem.slice_daemon run      # foreground (debug)

Protocol (newline-delimited JSON, one request per connection):
    Request:  {"query": "...", "scope": "...", "session_id": "...",
               "cwd": "...", "task_mode": "...", "use_llm": false}
    Response: {"ok": true,  "slice": "...", "elapsed_ms": 123}
         or   {"ok": false, "error": "...", "elapsed_ms": 123}

Anti-recursion: ``MEMEM_HOOK_DISABLE=1`` is set at startup so any subprocess
the daemon spawns (e.g. Haiku tournament calls) inherits it and does NOT
trigger memem hooks.
"""

from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

import structlog

from memem.active_slice import ActiveMemorySlice
from memem.models import MEMEM_DIR

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
LOCK_FILE = MEMEM_DIR / "slice.lock"
SOCK_FILE = MEMEM_DIR / "slice.sock"
PID_FILE = MEMEM_DIR / "slice-daemon.pid"
LOG_FILE = MEMEM_DIR / "slice-daemon.log"
HEARTBEAT_FILE = MEMEM_DIR / "slice-daemon.heartbeat"

# ---------------------------------------------------------------------------
# Runtime knobs
# ---------------------------------------------------------------------------
WORKER_THREADS = 1  # v1.8.0: single worker — active_slice_engine's tournament-cache
                    # write path is not thread-safe (file r/w/clobber). Single worker
                    # is still ~10x faster than the cold-start subprocess path because
                    # the model stays warm. v1.8.1 candidate: file-lock the cache + bump.
MAX_INFLIGHT = 8
REQUEST_TIMEOUT_SECONDS = 25.0

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_shutdown_requested = False
_lock_fh: Any | None = None
_inflight_counter_lock = threading.Lock()
_inflight_count = 0

# ---------------------------------------------------------------------------
# Per-session embedding + slice cache (topic-shift gate, Layer 3)
# ---------------------------------------------------------------------------
# Maps session_id → (embedding: list[float], slice: ActiveMemorySlice)
# Guarded by _session_cache_lock for thread safety (daemon is multithreaded).
_session_cache: dict[str, tuple[list[float], ActiveMemorySlice]] = {}
_session_cache_lock = threading.Lock()


def get_cached_query_embedding(session_id: str) -> list[float] | None:
    """Return the last cached query embedding for *session_id*, or None."""
    with _session_cache_lock:
        entry = _session_cache.get(session_id)
        return entry[0] if entry is not None else None


def get_cached_slice(session_id: str) -> ActiveMemorySlice | None:
    """Return the last cached ActiveMemorySlice for *session_id*, or None."""
    with _session_cache_lock:
        entry = _session_cache.get(session_id)
        return entry[1] if entry is not None else None


# NOTE: standalone half-helpers (set_cached_query_embedding, set_cached_slice) were
# removed in v1.9 — they silently dropped writes when the partner field wasn't
# already cached. Use set_cached_embedding_and_slice() for atomic writes.


def set_cached_embedding_and_slice(
    session_id: str,
    embedding: list[float],
    slice_obj: ActiveMemorySlice,
) -> None:
    """Atomically store both the embedding and the slice for *session_id*."""
    with _session_cache_lock:
        _session_cache[session_id] = (embedding, slice_obj)


def clear_session_cache(session_id: str) -> None:
    """Remove all cached data for *session_id* (e.g. for test teardown)."""
    with _session_cache_lock:
        _session_cache.pop(session_id, None)


log = structlog.get_logger("memem-slice-daemon")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Set up structlog → rotating file handler at ~/.memem/slice-daemon.log."""
    logger = logging.getLogger("memem-slice-daemon")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers):
        return
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    # v1.8.1: 0600 perms — log contains session IDs (post-creation chmod
    # to avoid umask races with concurrent threads/test fixtures).
    try:
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

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


# ---------------------------------------------------------------------------
# Lock helpers (mirroring miner_daemon._try_acquire_lock_once)
# ---------------------------------------------------------------------------

def _try_acquire_lock_once() -> bool | None:
    """Acquire slice.lock.

    Returns:
        True  — lock acquired
        False — held by a live process
        None  — stale lock removed; caller should retry
    """
    global _lock_fh
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o177)
    try:
        fh = open(LOCK_FILE, "a+")
    finally:
        os.umask(old_umask)
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fh.seek(0)
            content = fh.read().strip()
            pid = int(content.splitlines()[0]) if content else 0
        except (ValueError, IndexError, OSError):
            pid = 0
        fh.close()
        if pid > 0:
            try:
                os.kill(pid, 0)
                return False  # alive — genuine conflict
            except ProcessLookupError:
                # Dead — stale lock, remove and retry
                log.warning("stale_slice_lock_removed", stale_pid=pid, lock_file=str(LOCK_FILE))
                try:
                    LOCK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                return None  # retry
            except PermissionError:
                # v1.8.1: foreign-owned PID — genuine conflict (was previously
                # caught as OSError and treated as dead, letting two daemons
                # start on multi-user systems).
                return False
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _lock_fh = fh
    return True


def _acquire_lock() -> bool:
    result = _try_acquire_lock_once()
    if result is None:
        result = _try_acquire_lock_once()
        if result is None:
            return False
    return bool(result)


def _release_lock() -> None:
    global _lock_fh
    if _lock_fh is None:
        return
    try:
        _lock_fh.seek(0)  # type: ignore[union-attr]
        _lock_fh.truncate()  # type: ignore[union-attr]
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:
        pass
    try:
        _lock_fh.close()  # type: ignore[union-attr]
    except OSError:
        pass
    _lock_fh = None


# ---------------------------------------------------------------------------
# Stale socket cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale_socket() -> None:
    """Remove slice.sock if it doesn't belong to a live process.

    We check the lock file: if lock can be taken → daemon dead → socket stale.
    If we can't take the lock → another daemon is live → don't touch the socket.
    """
    if not SOCK_FILE.exists():
        return
    # Try a non-blocking lock attempt to see if there's a live daemon
    try:
        tmp = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(tmp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We got the lock — no live daemon, socket is stale
            fcntl.flock(tmp.fileno(), fcntl.LOCK_UN)
            tmp.close()
            log.info("removing_stale_socket", path=str(SOCK_FILE))
            SOCK_FILE.unlink(missing_ok=True)
        except BlockingIOError:
            # Lock held by live process — socket is live, leave it
            tmp.close()
    except OSError:
        # Can't open lock file — assume socket might be stale, remove it
        SOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------

def _write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------

def _request_shutdown(signum: int, frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    log.info("shutdown_requested", signal=signum)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def _write_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError as exc:
        log.warning("heartbeat_write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Model warm-up
# ---------------------------------------------------------------------------

def _warmup_model() -> None:
    """Attempt to import and touch sentence-transformers to pre-load weights."""
    try:
        # Try to import the embedding module — if it fails, log degraded mode.
        from memem import embedding_index  # noqa: F401
        log.info("model_loaded", note="embedding_index imported")
    except Exception as exc:
        log.warning("model_load_degraded", error=str(exc),
                    note="sentence-transformers unavailable; degraded mode")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _handle_one_request(conn: socket.socket) -> None:
    """Handle a single client connection (one request → one response → close)."""
    global _inflight_count
    t0 = time.monotonic()

    with _inflight_counter_lock:
        if _inflight_count >= MAX_INFLIGHT:
            _send_response(conn, {"ok": False, "error": "overloaded", "elapsed_ms": 0})
            conn.close()
            return
        _inflight_count += 1

    try:
        # Read one newline-terminated JSON line
        data = _recv_line(conn, timeout=5.0)
        if not data:
            _send_response(conn, {"ok": False, "error": "empty request", "elapsed_ms": 0})
            return

        try:
            req = json.loads(data)
        except json.JSONDecodeError as exc:
            _send_response(conn, {"ok": False, "error": f"bad JSON: {exc}", "elapsed_ms": 0})
            return

        query = str(req.get("query", "") or "")
        scope = str(req.get("scope", "default") or "default")
        session_id = str(req.get("session_id", "") or "")
        cwd = str(req.get("cwd", "") or "")
        task_mode = str(req.get("task_mode", "") or "")
        use_llm = bool(req.get("use_llm", False))

        environment: dict = {}
        if session_id:
            environment["session_id"] = session_id
        if cwd:
            environment["cwd"] = cwd
            environment["repo_path"] = cwd
        if task_mode:
            environment["task_mode"] = task_mode

        # MEMEM_INJECTION_MODE=tool silences the hook path. Daemon must mirror
        # the cli.py:slice gate so a running daemon doesn't keep injecting
        # context when tool mode is set. (v1.9 fix — Phase 4.5 finding A1)
        import memem.settings as _memem_settings
        if _memem_settings.MEMEM_INJECTION_MODE == "tool":
            elapsed = int((time.monotonic() - t0) * 1000)
            log.info("request_silenced_tool_mode", scope=scope, elapsed_ms=elapsed)
            _send_response(conn, {"ok": True, "slice": "", "elapsed_ms": elapsed})
            return

        # Generate slice — this is the hot path
        try:
            from memem.active_slice_engine import active_slice_response
            slice_text = active_slice_response(
                query,
                scope_id=scope,
                environment=environment or None,
                use_llm=use_llm,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.info("request_error", scope=scope, elapsed_ms=elapsed, error=type(exc).__name__)
            _send_response(conn, {"ok": False, "error": str(exc)[:200], "elapsed_ms": elapsed})
            return

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info("request_handled", scope=scope, elapsed_ms=elapsed)
        _write_heartbeat()
        _send_response(conn, {"ok": True, "slice": slice_text, "elapsed_ms": elapsed})

    finally:
        with _inflight_counter_lock:
            _inflight_count -= 1
        try:
            conn.close()
        except OSError:
            pass


def _recv_line(conn: socket.socket, timeout: float = 5.0) -> str:
    """Read until newline with a timeout. Returns decoded string (no newline)."""
    conn.settimeout(timeout)
    buf = bytearray()
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
            if len(buf) > 512 * 1024:
                raise OSError("request too large")
    except OSError:
        pass
    return buf.decode("utf-8", errors="replace").rstrip("\n").strip()


def _send_response(conn: socket.socket, data: dict) -> None:
    try:
        conn.settimeout(5.0)
        payload = json.dumps(data) + "\n"
        conn.sendall(payload.encode("utf-8"))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def run(
    sock_path: str | Path | None = None,
    max_inflight: int = MAX_INFLIGHT,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    lock_path: str | Path | None = None,
    pid_path: str | Path | None = None,
    heartbeat_path: str | Path | None = None,
) -> None:
    """Main loop — bind socket, accept connections, serve requests.

    This is the foreground entry point. Callers that want background operation
    should fork before calling this.

    Parameters allow tests to override socket path, lock path, and limits.
    lock_path, pid_path, heartbeat_path default to the MEMEM_DIR-based paths.
    """
    global MAX_INFLIGHT, REQUEST_TIMEOUT_SECONDS, _shutdown_requested, \
        LOCK_FILE, PID_FILE, HEARTBEAT_FILE

    _shutdown_requested = False

    # Override defaults from parameters
    MAX_INFLIGHT = max_inflight
    REQUEST_TIMEOUT_SECONDS = request_timeout

    if lock_path is not None:
        LOCK_FILE = Path(lock_path)
    if pid_path is not None:
        PID_FILE = Path(pid_path)
    if heartbeat_path is not None:
        HEARTBEAT_FILE = Path(heartbeat_path)

    _configure_logging()

    # Anti-recursion: set before any subprocess is spawned
    os.environ["MEMEM_HOOK_DISABLE"] = "1"

    # Determine socket path
    effective_sock = Path(sock_path) if sock_path else SOCK_FILE
    effective_sock.parent.mkdir(parents=True, exist_ok=True)

    # Acquire lock (single-instance enforcement)
    if not _acquire_lock():
        msg = f"Another slice daemon is running (lock: {LOCK_FILE})"
        print(msg)
        log.warning("slice_daemon_already_running", lock_file=str(LOCK_FILE))
        return

    try:
        _write_pid()
        _write_heartbeat()

        # Register signal handlers if running in the main thread (signal only
        # works in main thread; tests may invoke run() from a worker thread).
        try:
            signal.signal(signal.SIGTERM, _request_shutdown)
            signal.signal(signal.SIGINT, _request_shutdown)
        except ValueError:
            pass  # Non-main thread — signals not supported, skip

        log.info("daemon_started", pid=os.getpid(), sock=str(effective_sock))
        print(f"[slice-daemon] started (PID {os.getpid()}, sock {effective_sock})")

        # Anti-recursion env var is already set above; confirm here for log clarity
        # Warm up the embedding model at startup
        _warmup_model()
        log.info("model_loaded")

        # Clean up stale socket from previous run
        if effective_sock.exists():
            try:
                effective_sock.unlink()
            except OSError:
                pass

        # Bind Unix socket — 0600 perms via umask so other local users can't
        # connect and read/inject query content (privacy: requests carry the
        # user's full prompt as JSON payload).
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        old_umask = os.umask(0o177)
        try:
            try:
                server_sock.bind(str(effective_sock))
            except OSError as exc:
                log.error("socket_bind_failed", path=str(effective_sock), error=str(exc))
                _release_lock()
                return
        finally:
            os.umask(old_umask)

        server_sock.listen(16)
        server_sock.settimeout(1.0)  # allow shutdown polling

        with ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="slice-worker") as pool:
            while not _shutdown_requested:
                try:
                    conn, _ = server_sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if _shutdown_requested:
                        break
                    raise

                # Submit with per-request timeout enforcement. _handle_with_timeout
                # spawns an inner sub-thread for the actual handler so we can
                # kill+reply if it exceeds REQUEST_TIMEOUT_SECONDS.
                pool.submit(_handle_with_timeout, conn, REQUEST_TIMEOUT_SECONDS)

        log.info("daemon_shutdown")
        print("[slice-daemon] shut down cleanly")

    finally:
        try:
            server_sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        try:
            effective_sock.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        _release_lock()


# ---------------------------------------------------------------------------
# Per-request timeout enforcement wrapper
# ---------------------------------------------------------------------------

def _handle_with_timeout(conn: socket.socket, timeout: float) -> None:
    """Run _handle_one_request in a sub-thread; reply with timeout error if it takes too long.

    v1.8.1: do NOT use `with` on the inner ThreadPoolExecutor. The `with` exit
    blocks until the inner thread finishes, which means on a timeout the OUTER
    worker is pinned for another full `timeout` window (~50s total) — and with
    WORKER_THREADS=1 that monopolizes the entire daemon. Use `shutdown(wait=False)`
    so we return immediately on timeout; the inner thread continues running and
    self-cleans via its own `finally` (which decrements `_inflight_count`).
    """
    import concurrent.futures

    inner_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="slice-inner")
    try:
        fut = inner_pool.submit(_handle_one_request, conn)
        try:
            fut.result(timeout=timeout)
        except FuturesTimeoutError:
            log.warning("request_timeout", timeout_seconds=timeout)
            try:
                elapsed = int(timeout * 1000)
                _send_response(conn, {"ok": False, "error": "request timed out", "elapsed_ms": elapsed})
                conn.close()
            except OSError:
                pass
            # Critical: do NOT wait for the inner thread. Let it self-cleanup.
            inner_pool.shutdown(wait=False)
            return
        except Exception as exc:
            log.error("request_error_unhandled", error=str(exc))
    finally:
        # Only reached on the non-timeout paths. wait=False is safe — the
        # successful future has already returned, the thread is idle.
        inner_pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def start_daemon() -> None:
    """Start slice daemon as a background process (double-fork)."""
    _configure_logging()

    existing = _read_pid()
    if existing:
        print(f"Slice daemon already running (PID {existing})")
        return

    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for PID file
        child_pid = None
        for _ in range(20):
            time.sleep(0.1)
            child_pid = _read_pid()
            if child_pid:
                break
        if child_pid:
            print(f"Slice daemon started (PID {child_pid})")
        else:
            print(f"Failed to start daemon — check {LOG_FILE}")
        return

    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect stdio
    devnull_r = open(os.devnull)
    devnull_w = open(os.devnull, "w")
    os.dup2(devnull_r.fileno(), sys.stdin.fileno())
    os.dup2(devnull_w.fileno(), sys.stdout.fileno())
    os.dup2(devnull_w.fileno(), sys.stderr.fileno())
    devnull_r.close()
    devnull_w.close()

    run()


def stop_daemon() -> None:
    """Stop the running daemon: SIGTERM, poll for death, escalate to SIGKILL.

    v1.8.1: previously this sent one SIGTERM and returned immediately, which
    raced the lock on quick `stop && start` if the daemon was mid-model-load
    or mid-request. Mirror miner-wrapper's _kill_with_escalation pattern.
    """
    pid = _read_pid()
    if not pid:
        print("Slice daemon not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        print("Slice daemon was not running (stale PID file cleaned)")
        return

    # Poll up to 5s for graceful exit
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"Slice daemon stopped (PID {pid})")
            return
        time.sleep(0.25)

    # Escalate to SIGKILL
    print(f"Slice daemon did not stop after SIGTERM — escalating to SIGKILL (PID {pid})")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        print(f"Slice daemon stopped (PID {pid})")
        return

    # Brief poll for SIGKILL to land
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"Slice daemon killed (PID {pid})")
            return
        time.sleep(0.1)

    print(f"ERROR: Slice daemon (PID {pid}) still alive after SIGKILL")
    sys.exit(1)


def status_daemon() -> None:
    """Print whether the daemon is running."""
    pid = _read_pid()
    if pid:
        heartbeat = ""
        if HEARTBEAT_FILE.exists():
            try:
                ts = int(HEARTBEAT_FILE.read_text().strip())
                age = int(time.time()) - ts
                heartbeat = f", last heartbeat {age}s ago"
            except Exception:
                pass
        print(f"Slice daemon running (PID {pid}{heartbeat})")
    else:
        print("Slice daemon not running")


# ---------------------------------------------------------------------------
# Module entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"

    if cmd == "start":
        start_daemon()
    elif cmd == "stop":
        stop_daemon()
    elif cmd == "status":
        status_daemon()
    elif cmd == "run":
        # Foreground mode for debugging
        _configure_logging()
        _cleanup_stale_socket()
        run()
    else:
        print("Usage: python3 -m memem.slice_daemon [start|stop|status|run]")
        sys.exit(1)
