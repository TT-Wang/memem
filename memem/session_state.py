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
DEFAULT_EXCLUDED_SESSION_PROJECTS = ("-home-claude-user-lexie",)


def _excluded_session_projects() -> set[str]:
    raw = _env(
        "MEMEM_EXCLUDED_SESSION_PROJECTS",
        "CORTEX_EXCLUDED_SESSION_PROJECTS",
        default=":".join(DEFAULT_EXCLUDED_SESSION_PROJECTS),
    ).strip()
    if raw.lower() in {"", "0", "false", "none"}:
        return set()
    return {item.strip() for item in raw.replace(",", ":").split(":") if item.strip()}


def _project_dir_for_session(jsonl_path: Path, sessions_dir: Path) -> str:
    try:
        rel = jsonl_path.relative_to(sessions_dir)
    except ValueError:
        return ""
    return rel.parts[0] if rel.parts else ""


def _is_excluded_session_project(jsonl_path: Path, sessions_dir: Path) -> bool:
    project_dir = _project_dir_for_session(jsonl_path, sessions_dir)
    return bool(project_dir and project_dir in _excluded_session_projects())


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


# memem's own subprocess calls to `claude -p --model haiku` are recorded
# by Claude Code as regular sessions in whatever project directory the
# parent was running from. Their first user message always starts with
# one of these prompt signatures — we detect them here so the miner
# doesn't recursively try to mine its own subprocess fossils.
#
# Each signature is the exact prefix of the corresponding prompt body
# built by one of the memem subprocess call sites:
#
#   - "Below is a coding conversation"     -> mining._summarize_session_haiku
#   - "EXISTING:"                          -> mining._merge_memories
#   - "You were asked to extract memories" -> mining corrective retry
#   - "QUERY: "                            -> assembly.context_assemble
#   - "Review these memory entries"        -> assembly._consolidate_project
#   - "USER MESSAGE:\n"                    -> recall.smart_recall
#   - "# <project> — Project Playbook"     -> playbook._playbook_refine
_MEMEM_SUBPROCESS_PROMPT_PREFIXES = (
    "Below is a coding conversation",
    "EXISTING:",
    "You were asked to extract memories",
    "QUERY: ",
    "Review these memory entries",
    "USER MESSAGE:\n",
)

# The playbook refine prompt doesn't have a clean prefix-string match
# (it starts with "# {project} — Project Playbook" where {project} is
# arbitrary), so we detect it by looking for the literal substring
# "— Project Playbook" in the first ~120 chars of the user message.
_PLAYBOOK_REFINE_MARKER = "— Project Playbook"


def _looks_like_memem_subprocess(jsonl_path: Path) -> bool:
    """True if a session's first human message matches a memem prompt signature.

    Cheap path-independent filter to catch memem's own subprocess calls
    to `claude -p` — these subprocesses inherit the parent's cwd and
    end up recorded in the normal project directory alongside real user
    sessions, so a path-based filter like `-root/` doesn't catch them.

    Reads at most the first 20 JSONL lines (usually only 1-2 are needed)
    to keep scan-time overhead under a few milliseconds per session.
    Returns False on any parse/read failure so a corrupt session file
    doesn't accidentally get filtered as a false positive.
    """
    try:
        with open(jsonl_path, encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    return False
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if entry.get("type") != "user":
                    continue
                content = entry.get("message", {}).get("content", "")
                # content can be a bare string OR a list of content blocks
                text: str = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break
                if not text:
                    return False
                head = text.lstrip()[:200]
                return any(
                    head.startswith(prefix)
                    for prefix in _MEMEM_SUBPROCESS_PROMPT_PREFIXES
                ) or (head.startswith("# ") and _PLAYBOOK_REFINE_MARKER in head)
    except OSError:
        return False
    return False


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
            if _is_excluded_session_project(jsonl_path, sessions_dir):
                continue
            if "/subagents/" in str(jsonl_path):
                continue
            # Skip sessions under the `-root` project. `/.claude/projects/-root/`
            # is where Claude Code stores headless `claude -p` subprocess
            # invocations — memem's own Haiku mining calls end up there. If
            # we don't filter, the miner recursively picks up its own
            # subprocess sessions, feeds them back to Haiku, and burns
            # quota mining prompt templates. Found 2026-04-15: 1448 of
            # 5546 "successfully mined" sessions (26%) were self-referential
            # subprocess artifacts, yielding 110 polluted memories (9.9%
            # of the vault). Filtering one directory fixes both the bug
            # and prevents it from recurring.
            if "/projects/-root/" in str(jsonl_path):
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
            # Content-signature filter: skip memem's own subprocess fossils
            # (mining / merge / assemble / smart_recall / consolidation calls
            # to `claude -p` that got recorded as normal session files in the
            # parent's project directory). The -root filter above catches
            # headless runs from /root; this one catches subprocess calls
            # that inherit the miner daemon's cwd (e.g. the memem repo dir).
            # See _looks_like_memem_subprocess for the prompt signatures.
            if _looks_like_memem_subprocess(jsonl_path):
                continue
            settled.append((int(stat.st_mtime_ns), jsonl_path))

    settled.sort(key=lambda item: item[0])
    return [path for _, path in settled]
