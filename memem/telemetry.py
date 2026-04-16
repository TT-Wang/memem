"""Access telemetry and event log for Cortex.

Telemetry sidecar tracks access counts and last-accessed timestamps
without rewriting memory files on every read. Event log is an
append-only audit trail of all memory operations.
"""

import fcntl
import json
import logging
import os

from memem.models import EVENT_LOG, MEMEM_DIR, TELEMETRY_FILE, now_iso

log = logging.getLogger("memem-telemetry")


# ============================================================================
# Access telemetry sidecar
# ============================================================================

def _get_telemetry(memory_id: str) -> dict:
    """Get access telemetry for a memory from the sidecar file."""
    if not TELEMETRY_FILE.exists():
        return {"access_count": 0, "last_accessed": ""}
    try:
        data = json.loads(TELEMETRY_FILE.read_text())
        entry = data.get(memory_id[:8], {})
        return {
            "access_count": entry.get("access_count", 0),
            "last_accessed": entry.get("last_accessed", ""),
        }
    except (json.JSONDecodeError, OSError):
        return {"access_count": 0, "last_accessed": ""}


def _record_access(memory_id: str) -> None:
    """Record a memory access in the telemetry sidecar file.

    Write is atomic (tmp + fsync + os.replace). If the existing file is
    corrupt, it's preserved as <file>.corrupt.<ts> rather than silently
    overwritten so telemetry history can be recovered if needed.
    """
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TELEMETRY_FILE.with_suffix(".lock")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = {}
        if TELEMETRY_FILE.exists():
            try:
                data = json.loads(TELEMETRY_FILE.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                backup = TELEMETRY_FILE.with_suffix(f".corrupt.{int(os.path.getmtime(TELEMETRY_FILE))}")
                try:
                    TELEMETRY_FILE.rename(backup)
                    log.warning("telemetry file corrupt (%s); preserved as %s", exc, backup.name)
                except OSError:
                    log.warning("telemetry file corrupt (%s); could not back up", exc)
                data = {}
        key = memory_id[:8]
        entry = data.get(key, {"access_count": 0, "last_accessed": ""})
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["last_accessed"] = now_iso()
        data[key] = entry

        # Atomic write: tmp + fsync + os.replace
        tmp_path = TELEMETRY_FILE.with_suffix(".tmp")
        with open(tmp_path, "w") as out:
            out.write(json.dumps(data))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, TELEMETRY_FILE)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ============================================================================
# Session-level recall tracking (closed-loop relevance scoring)
# ============================================================================

_SESSION_RECALLS_FILE = MEMEM_DIR / "session_recalls.json"


def record_session_recall(session_id: str, memory_id: str) -> None:
    """Record that a memory was recalled during a specific session.

    Deduplicates within a session — calling twice with the same
    ``(session_id, memory_id)`` pair writes only one entry. Atomic
    writes via tmp + fsync + os.replace.
    """
    if not session_id or not memory_id:
        return
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _SESSION_RECALLS_FILE.with_suffix(".lock")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data: dict = {}
        if _SESSION_RECALLS_FILE.exists():
            try:
                data = json.loads(_SESSION_RECALLS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}

        key = session_id[:12]
        mid = memory_id[:8]
        recalls = data.get(key, [])
        if mid not in recalls:
            recalls.append(mid)
            data[key] = recalls

            tmp_path = _SESSION_RECALLS_FILE.with_suffix(".tmp")
            with open(tmp_path, "w") as out:
                out.write(json.dumps(data))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_path, _SESSION_RECALLS_FILE)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def get_session_recalls(session_id: str) -> list[str]:
    """Return the list of memory IDs recalled during a session."""
    if not _SESSION_RECALLS_FILE.exists():
        return []
    try:
        data = json.loads(_SESSION_RECALLS_FILE.read_text())
        return data.get(session_id[:12], [])
    except (json.JSONDecodeError, OSError):
        return []


# ============================================================================
# Event log (audit trail)
# ============================================================================

def _log_event(op: str, memory_id: str = "", **details) -> None:
    """Append an event to the audit log. Atomic for lines < PIPE_BUF."""
    try:
        MEMEM_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"op": op, "memory_id": memory_id, "timestamp": now_iso(), **details}
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Non-fatal — don't crash operations for logging failures
