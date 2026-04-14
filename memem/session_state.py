import fcntl
import json
import os
import time
from pathlib import Path

from memem.miner_protocol import MINER_STATE_VERSION, STATUS_COMPLETE
from memem.models import (
    MEMEM_DIR,
    _env,
    _now,
)

MINED_SESSIONS_FILE = MEMEM_DIR / ".mined_sessions"
SESSIONS_DIRS = [Path.home() / ".claude" / "projects"]
_extra = _env("MEMEM_EXTRA_SESSION_DIRS", "CORTEX_EXTRA_SESSION_DIRS")
if _extra:
    SESSIONS_DIRS.extend(Path(path) for path in _extra.split(":") if path)

SETTLE_SECONDS = int(_env("MEMEM_MINER_SETTLE_SECONDS", "CORTEX_MINER_SETTLE_SECONDS", default="300"))


def session_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _parse_state_line(line: str) -> dict | None:
    raw = line.strip()
    if not raw or not raw.startswith("{"):
        return None

    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    session_id = str(state.get("session_id", "")).strip()
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "status": str(state.get("status", STATUS_COMPLETE)),
        "mtime_ns": state.get("mtime_ns"),
        "size": state.get("size"),
        "version": str(state.get("version", MINER_STATE_VERSION)),
        "updated_at": str(state.get("updated_at", "")),
        "message": str(state.get("message", "")),
    }


def load_mined_session_state() -> dict[str, dict]:
    if not MINED_SESSIONS_FILE.exists():
        return {}

    states: dict[str, dict] = {}
    try:
        for line in MINED_SESSIONS_FILE.read_text().splitlines():
            state = _parse_state_line(line)
            if state:
                states[state["session_id"]] = state
    except OSError:
        return {}
    return states


def save_mined_session_state(states: dict[str, dict]) -> None:
    MINED_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = MINED_SESSIONS_FILE.with_suffix(".tmp")
    lines = []
    for session_id in sorted(states):
        state = dict(states[session_id])
        state["session_id"] = session_id
        lines.append(json.dumps(state, sort_keys=True))
    content = ("\n".join(lines) + "\n") if lines else ""
    # Atomic write: fsync before rename so a crash can't leave an empty file.
    with open(temp_path, "w") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp_path, MINED_SESSIONS_FILE)


def update_session_state(path: Path, status: str, message: str = "") -> dict:
    lock_path = MINED_SESSIONS_FILE.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        states = load_mined_session_state()
        fingerprint = session_fingerprint(path)
        session_id = path.stem
        state = {
            "session_id": session_id,
            "status": status,
            "mtime_ns": fingerprint["mtime_ns"],
            "size": fingerprint["size"],
            "version": MINER_STATE_VERSION,
            "updated_at": _now(),
            "message": message[:500],
        }
        states[session_id] = state
        save_mined_session_state(states)
        return state
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def session_is_complete(path: Path, state: dict | None) -> bool:
    if not state or state.get("status") != STATUS_COMPLETE:
        return False
    if str(state.get("version", "")) != MINER_STATE_VERSION:
        return False

    try:
        fingerprint = session_fingerprint(path)
    except OSError:
        return False

    return (
        state.get("mtime_ns") == fingerprint["mtime_ns"]
        and state.get("size") == fingerprint["size"]
    )


INSTALLED_AT_FILE = MEMEM_DIR / ".installed_at"


def _get_installed_at() -> float:
    """Read the install-time gate. Returns 0.0 if missing — does NOT recreate.

    v0.10.2 fix: this used to lazily recreate the marker on read, which broke
    `--mine-all` (the CLI would clear the marker, then the immediate scan would
    recreate it with the current timestamp and filter out all older sessions).
    Callers that need to ensure the marker exists must call ``_ensure_installed_at``
    explicitly.
    """
    if not INSTALLED_AT_FILE.exists():
        return 0.0
    try:
        return float(INSTALLED_AT_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _ensure_installed_at() -> float:
    """Create the install-time gate if missing, return its timestamp.

    Called by bootstrap / first-daemon-boot paths that need to establish
    the "only mine sessions after this moment" gate for normal operation.
    Explicitly separate from the read-only ``_get_installed_at`` so
    ``--mine-all`` can clear the marker without it being silently rebuilt.
    """
    if not INSTALLED_AT_FILE.exists():
        MEMEM_DIR.mkdir(parents=True, exist_ok=True)
        INSTALLED_AT_FILE.write_text(str(time.time()))
    return _get_installed_at()


def clear_installed_at():
    """Remove the installed_at gate so all sessions are mined (used by --mine-all)."""
    INSTALLED_AT_FILE.unlink(missing_ok=True)


def find_settled_sessions(
    states: dict[str, dict] | None = None,
    bypass_gate: bool = False,
) -> list[Path]:
    """Find session JSONLs settled long enough to mine.

    ``bypass_gate=True`` skips the install-time filter entirely — used by
    ``--mine-all`` to process pre-install history. Default behavior respects
    the marker if present, but does NOT recreate it on first read (callers
    that want the gate must call ``_ensure_installed_at`` first).
    """
    now = time.time()
    states = states or {}
    settled: list[tuple[int, Path]] = []
    installed_at = 0.0 if bypass_gate else _get_installed_at()

    for sessions_dir in SESSIONS_DIRS:
        if not sessions_dir.exists():
            continue
        for jsonl_path in sessions_dir.rglob("*.jsonl"):
            if "/subagents/" in str(jsonl_path):
                continue
            try:
                stat = jsonl_path.stat()
            except OSError:
                continue
            if stat.st_size < 5000:
                continue
            # Only mine sessions created after install (unless gate cleared)
            if installed_at > 0 and stat.st_mtime < installed_at:
                continue
            if (now - stat.st_mtime) <= SETTLE_SECONDS:
                continue
            if session_is_complete(jsonl_path, states.get(jsonl_path.stem)):
                continue
            settled.append((int(stat.st_mtime_ns), jsonl_path))

    settled.sort(key=lambda item: item[0])
    return [path for _, path in settled]
