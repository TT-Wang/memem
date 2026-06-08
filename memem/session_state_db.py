"""SQLite WAL backend for mined-session state.

Replaces the JSONL-based persistence in session_state.py with a WAL-mode
SQLite database. Public API is identical to the original module so
session_state.py can delegate transparently.

Why no fcntl:
    SQLite WAL provides single-writer/many-reader semantics with
    busy_timeout=5000ms; an additional fcntl lock around the DB connection
    would be redundant and could actually cause deadlocks (sqlite3 module
    releases the GIL during I/O, so two threads could hold the fcntl lock
    at the same time). Let SQLite handle concurrency.
"""

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from memem.models import MEMEM_DIR, _now
from memem.session_state import MINER_STATE_VERSION

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# The old JSONL file (imported here so migration code can reference it
# without a circular import through session_state.py).
_JSONL_FILE = MEMEM_DIR / ".mined_sessions"
DB_PATH = MEMEM_DIR / "mined_sessions.db"

_ENSURE_LOCK = threading.Lock()

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mined_sessions (
    session_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    mtime_ns     INTEGER NOT NULL,
    size         INTEGER NOT NULL,
    version      TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    message      TEXT,
    offset_bytes INTEGER NOT NULL DEFAULT 0
);
"""

_ADD_OFFSET_BYTES_SQL = "ALTER TABLE mined_sessions ADD COLUMN offset_bytes INTEGER NOT NULL DEFAULT 0;"
_ADD_TIMEOUT_FAILURES_SQL = "ALTER TABLE mined_sessions ADD COLUMN timeout_failures INTEGER NOT NULL DEFAULT 0;"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL pragmas applied before any query.

    Order matters: busy_timeout must be set FIRST so that subsequent PRAGMA
    and DDL statements wait up to 5 s instead of failing immediately if
    another connection holds the write lock.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout first so subsequent PRAGMAs don't race on a write lock
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _backup_corrupt_jsonl(jsonl_path: Path) -> None:
    """Copy jsonl_path to a timestamped .corrupt.* backup and warn on stderr.

    Mirrors the logic in session_state._backup_corrupt_file so that the same
    corruption-detection behaviour is preserved during migration.
    """
    timestamp = int(time.time())
    backup = jsonl_path.with_name(f".mined_sessions.corrupt.{timestamp}")
    shutil.copy2(jsonl_path, backup)
    print(
        f"WARNING: corrupt session state file backed up to {backup}; starting fresh",
        file=sys.stderr,
    )


_INITIALIZED: set[str] = set()  # fully-initialized DB paths (table + migration done)


def _ensure_db(db_path: Path) -> None:
    """Create the DB schema (and run JSONL migration) if not already done.

    Uses _ENSURE_LOCK + _INITIALIZED set so concurrent threads don't race on
    DDL or migration.  The fast path (already in _INITIALIZED) is a cheap
    set-membership test; everything else serializes under the lock.
    """
    key = str(db_path)
    if key in _INITIALIZED:
        return

    with _ENSURE_LOCK:
        # Re-check inside the lock: another thread may have finished
        # initialization while we were waiting.
        if key in _INITIALIZED:
            return

        # Create the DB and table.
        with _connect(db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)

            # Add offset_bytes column to existing DBs that predate this column.
            try:
                conn.execute(_ADD_OFFSET_BYTES_SQL)
            except sqlite3.OperationalError:
                # "duplicate column name: offset_bytes" — already migrated
                pass

            # Add timeout_failures column to existing DBs that predate v1.7.
            try:
                conn.execute(_ADD_TIMEOUT_FAILURES_SQL)
            except sqlite3.OperationalError:
                # "duplicate column name: timeout_failures" — already migrated
                pass

            # Check for an existing JSONL to migrate.
            jsonl_path = db_path.parent / ".mined_sessions"
            if jsonl_path.exists():
                _migrate_jsonl(conn, jsonl_path)

        # Mark as initialized so the fast path fires on subsequent calls.
        _INITIALIZED.add(key)


def _migrate_jsonl(conn: sqlite3.Connection, jsonl_path: Path) -> None:
    """Migrate data from the legacy JSONL file into `conn` and rename it .legacy.

    Called only from inside _ENSURE_LOCK while the table-creation transaction
    is open, so there is no race with concurrent writers.
    """
    # Import _parse_state_line lazily to avoid circular import at module load
    # time.  session_state imports us, so we can't import it at the top.
    from memem.session_state import _parse_state_line  # noqa: PLC0415

    try:
        raw_text = jsonl_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        # Can't read — rename to legacy anyway and continue with empty DB.
        jsonl_path.rename(jsonl_path.with_suffix(".legacy"))
        return

    # v2.1.0: content-sniff guard. mine_delta._record_mined_session writes
    # .mined_sessions as a plain list of session IDs (one per line), not as
    # JSONL state records. Without this guard, _parse_state_line returns None
    # for every line, the corrupt-backup logic fires, and the v2.1.0 dedup
    # state file is destroyed on every server boot. Only skip migration when
    # ALL non-blank lines look like session-ID strings (UUID-ish: alphanum +
    # `.-_/`). Binary garbage and partial-JSON corruption will NOT match,
    # so the existing corrupt-backup path still fires for real corruption.
    import re  # noqa: PLC0415
    _id_pat = re.compile(r"^[a-zA-Z0-9._/-]+$")
    nonblank = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if nonblank and all(_id_pat.match(ln) for ln in nonblank):
        # All lines are session-ID-shaped — this is the v2.1.0 plain-list
        # format, not a stale v1.x JSONL. Skip migration entirely.
        return

    rows = []
    for line in raw_text.splitlines():
        entry = _parse_state_line(line)
        if entry:
            # Migrated rows: last_error mirrors message (legacy schema had no
            # separate last_error field). Live writes populate them independently.
            entry["last_error"] = entry.get("message", "")
            rows.append(entry)

    # If the file was non-empty but zero lines parsed, treat as corrupt.
    if raw_text.strip() and not rows:
        _backup_corrupt_jsonl(jsonl_path)
        jsonl_path.rename(jsonl_path.with_suffix(".legacy"))
        return

    if rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO mined_sessions
                (session_id, status, attempts, last_error,
                 mtime_ns, size, version, updated_at, message)
            VALUES
                (:session_id, :status, :attempts, :last_error,
                 :mtime_ns, :size, :version, :updated_at, :message)
            """,
            rows,
        )

    # Rename .mined_sessions → .mined_sessions.legacy as a safety net.
    # (Keep it; do NOT delete it.)
    if jsonl_path.exists():
        jsonl_path.rename(jsonl_path.with_suffix(".legacy"))


# ---------------------------------------------------------------------------
# Public API — mirrors session_state.py's persistence functions
# ---------------------------------------------------------------------------


def load_mined_session_state(db_path: Path | None = None) -> dict[str, dict]:
    """Load all session state rows from the SQLite DB.

    Returns a dict keyed by session_id, matching the shape previously
    produced by the JSONL loader so all callers work without changes.
    """
    if db_path is None:
        db_path = DB_PATH
    _ensure_db(db_path)
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT session_id, status, attempts, mtime_ns, size, version,"
                "       updated_at, message, offset_bytes, timeout_failures FROM mined_sessions"
            ).fetchall()
    except sqlite3.Error:
        return {}

    return {
        row["session_id"]: {
            "session_id": row["session_id"],
            "status": row["status"],
            "attempts": row["attempts"],
            "mtime_ns": row["mtime_ns"],
            "size": row["size"],
            "version": row["version"],
            "updated_at": row["updated_at"],
            "message": row["message"] or "",
            "offset_bytes": int(row["offset_bytes"] or 0),
            "timeout_failures": int(row["timeout_failures"] or 0),
        }
        for row in rows
    }


def save_mined_session_state(
    states: dict[str, dict], db_path: Path | None = None
) -> None:
    """Bulk-replace the entire session state table.

    Used by callers that previously wrote the whole dict at once (e.g. tests
    that pre-populate state).  For incremental updates prefer
    ``update_session_state``.
    """
    if db_path is None:
        db_path = DB_PATH
    _ensure_db(db_path)
    try:
        with _connect(db_path) as conn:
            conn.execute("DELETE FROM mined_sessions;")
            rows = []
            for session_id, state in states.items():
                rows.append(
                    {
                        "session_id": session_id,
                        "status": state.get("status", ""),
                        "attempts": int(state.get("attempts", 0)),
                        "last_error": state.get("message", "")[:500],
                        "mtime_ns": int(state.get("mtime_ns", 0)),
                        "size": int(state.get("size", 0)),
                        "version": str(state.get("version", MINER_STATE_VERSION)),
                        "updated_at": str(state.get("updated_at", _now())),
                        "message": state.get("message", "")[:500],
                        "offset_bytes": int(state.get("offset_bytes", 0)),
                        "timeout_failures": int(state.get("timeout_failures", 0)),
                    }
                )
            if rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO mined_sessions
                        (session_id, status, attempts, last_error,
                         mtime_ns, size, version, updated_at, message, offset_bytes,
                         timeout_failures)
                    VALUES
                        (:session_id, :status, :attempts, :last_error,
                         :mtime_ns, :size, :version, :updated_at, :message, :offset_bytes,
                         :timeout_failures)
                    """,
                    rows,
                )
    except sqlite3.Error:
        pass


def update_session_state(
    path: Path,
    status: str,
    message: str = "",
    attempts: int = 0,
    offset_bytes: int = 0,
    timeout_failures: int = 0,
    db_path: Path | None = None,
) -> dict:
    """Upsert a single session record and return the new state dict.

    SQLite WAL handles concurrent writers via busy_timeout=5000ms, so no
    additional fcntl lock is required.
    """
    from memem.session_state import session_fingerprint  # noqa: PLC0415

    if db_path is None:
        db_path = DB_PATH
    _ensure_db(db_path)

    fingerprint = session_fingerprint(path)
    session_id = path.stem
    now_str = _now()
    truncated_message = message[:500]

    row = {
        "session_id": session_id,
        "status": status,
        "attempts": int(attempts),
        "last_error": truncated_message,
        "mtime_ns": fingerprint["mtime_ns"],
        "size": fingerprint["size"],
        "version": MINER_STATE_VERSION,
        "updated_at": now_str,
        "message": truncated_message,
        "offset_bytes": int(offset_bytes),
        "timeout_failures": int(timeout_failures),
    }

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO mined_sessions
                (session_id, status, attempts, last_error,
                 mtime_ns, size, version, updated_at, message, offset_bytes,
                 timeout_failures)
            VALUES
                (:session_id, :status, :attempts, :last_error,
                 :mtime_ns, :size, :version, :updated_at, :message, :offset_bytes,
                 :timeout_failures)
            """,
            row,
        )

    return {
        "session_id": session_id,
        "status": status,
        "attempts": int(attempts),
        "mtime_ns": fingerprint["mtime_ns"],
        "size": fingerprint["size"],
        "version": MINER_STATE_VERSION,
        "updated_at": now_str,
        "message": truncated_message,
        "offset_bytes": int(offset_bytes),
        "timeout_failures": int(timeout_failures),
    }
