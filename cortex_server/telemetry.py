"""Access telemetry and event log for Cortex.

Telemetry sidecar tracks access counts and last-accessed timestamps
without rewriting memory files on every read. Event log is an
append-only audit trail of all memory operations.
"""

import fcntl
import json
import logging

from cortex_server.models import CORTEX_DIR, EVENT_LOG, TELEMETRY_FILE, now_iso

log = logging.getLogger("cortex-telemetry")


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
    """Record a memory access in the telemetry sidecar file."""
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TELEMETRY_FILE.with_suffix(".lock")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = {}
        if TELEMETRY_FILE.exists():
            try:
                data = json.loads(TELEMETRY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        key = memory_id[:8]
        entry = data.get(key, {"access_count": 0, "last_accessed": ""})
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["last_accessed"] = now_iso()
        data[key] = entry
        TELEMETRY_FILE.write_text(json.dumps(data))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ============================================================================
# Event log (audit trail)
# ============================================================================

def _log_event(op: str, memory_id: str = "", **details) -> None:
    """Append an event to the audit log. Atomic for lines < PIPE_BUF."""
    try:
        CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"op": op, "memory_id": memory_id, "timestamp": now_iso(), **details}
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Non-fatal — don't crash operations for logging failures
