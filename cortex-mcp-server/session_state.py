import fcntl
import json
import os
import time
from pathlib import Path

from miner_protocol import MINER_STATE_VERSION, STATUS_COMPLETE
from storage import CORTEX_DIR, _now


MINED_SESSIONS_FILE = CORTEX_DIR / ".mined_sessions"
SESSIONS_DIRS = [Path.home() / ".claude" / "projects"]
_extra = os.environ.get("CORTEX_EXTRA_SESSION_DIRS", "")
if _extra:
    SESSIONS_DIRS.extend(Path(path) for path in _extra.split(":") if path)

SETTLE_SECONDS = int(os.environ.get("CORTEX_MINER_SETTLE_SECONDS", "300"))


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
    temp_path.write_text(content)
    temp_path.replace(MINED_SESSIONS_FILE)


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


INSTALLED_AT_FILE = CORTEX_DIR / ".installed_at"


def _get_installed_at() -> float:
    """Get the timestamp when Cortex was installed. Returns 0 if mine-all mode."""
    if not INSTALLED_AT_FILE.exists():
        # First run — record install time
        CORTEX_DIR.mkdir(parents=True, exist_ok=True)
        INSTALLED_AT_FILE.write_text(str(time.time()))
    try:
        return float(INSTALLED_AT_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def clear_installed_at():
    """Remove the installed_at gate so all sessions are mined (used by --mine-all)."""
    INSTALLED_AT_FILE.unlink(missing_ok=True)


def find_settled_sessions(states: dict[str, dict] | None = None) -> list[Path]:
    now = time.time()
    states = states or {}
    settled: list[tuple[int, Path]] = []
    installed_at = _get_installed_at()

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
