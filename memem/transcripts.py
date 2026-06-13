import json
import os
import re
import sqlite3
import time
from pathlib import Path

import structlog

from memem.session_state import SESSIONS_DIRS

log = structlog.get_logger("memem-transcripts")

# ---------------------------------------------------------------------------
# Grep fallback safety caps — prevent hanging on large session dirs
# ---------------------------------------------------------------------------

MAX_FALLBACK_FILE_BYTES = 10 * 1024 * 1024  # skip files larger than 10 MiB
MAX_FALLBACK_FILES = 500                     # stop after scanning this many files
FALLBACK_TIME_BUDGET_S = 5.0                 # hard wall-clock limit for grep fallback

# ---------------------------------------------------------------------------
# FTS5 index for transcript search
# ---------------------------------------------------------------------------

_CREATE_TRANSCRIPTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts USING fts5(
    session_id UNINDEXED,
    session_date UNINDEXED,
    project UNINDEXED,
    user_text UNINDEXED,
    assistant_text UNINDEXED,
    content
)
"""


def _db_path() -> Path:
    """Return transcript FTS DB path, resolved from MEMEM_DIR at call time."""
    state_dir = os.environ.get("MEMEM_DIR") or os.environ.get("CORTEX_DIR")
    if state_dir:
        return Path(state_dir) / "transcript_fts.db"
    from memem.models import MEMEM_DIR as _MEMEM_DIR
    return _MEMEM_DIR / "transcript_fts.db"


def _open_fts_db() -> sqlite3.Connection:
    """Open (and create if needed) the transcript FTS5 database.

    Detects old single-row-per-session schema (missing user_text / assistant_text
    columns) and drops + recreates the table so the per-pair schema is enforced.
    """
    db_file = _db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")

    # Check if the table exists and has the new schema (user_text column).
    # FTS5 tables store column info in the fts5_data table; simplest check is
    # to see if a SELECT on user_text succeeds.
    needs_rebuild = False
    try:
        conn.execute("SELECT user_text FROM transcripts LIMIT 0")
    except sqlite3.OperationalError:
        # Either table doesn't exist yet, or it has the old schema — either
        # way we need to (re)create it.
        needs_rebuild = True

    if needs_rebuild:
        # Drop old table if it exists (schema migration)
        conn.execute("DROP TABLE IF EXISTS transcripts")
        conn.execute(_CREATE_TRANSCRIPTS_TABLE)
        conn.commit()
    else:
        # Table already has correct schema; ensure it exists via IF NOT EXISTS
        conn.execute(_CREATE_TRANSCRIPTS_TABLE)
        conn.commit()

    return conn


def _fts5_escape_query(query: str) -> str:
    """Escape a user query for safe FTS5 MATCH use.

    Wraps each whitespace-separated term in double quotes so that FTS5
    operators (AND, OR, NOT, NEAR, ^, *) and unbalanced quotes in user
    input are treated as literal strings rather than query syntax.

    Examples:
        'foo bar'          → '"foo" "bar"'
        'foo AND bar'      → '"foo" "AND" "bar"'
        'test "unbalanced' → '"test" "unbalanced"'
    """
    terms = query.split()
    if not terms:
        return '""'
    escaped_terms = []
    for term in terms:
        # Strip any existing quotes; re-wrap in double quotes.
        clean = term.replace('"', '')
        if clean:
            escaped_terms.append(f'"{clean}"')
    return " ".join(escaped_terms) if escaped_terms else '""'


def index_session(path: Path) -> None:
    """Index one JSONL session file into the FTS5 database.

    Stores ONE ROW PER Q/A turn-pair so that _transcript_search_fts returns
    per-exchange granularity matching the grep fallback path.

    Uses _parse_jsonl_session_pairs (the canonical pair parser) to extract
    turn pairs. Deletes all existing rows for this session_id before inserting
    fresh rows (idempotent re-index: same file → same rows, no duplicates).
    """
    session_id = path.stem  # filename without extension
    # Derive session_date from mtime (best effort)
    try:
        mtime = path.stat().st_mtime
        import datetime as _dt
        session_date = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        session_date = ""
    # Derive project from parent directory name (same logic as mine_delta)
    try:
        parent_dir = path.parent.name
        home = str(Path.home())
        munged_home = home.replace("/", "-")
        if parent_dir == munged_home:
            project = "general"
        elif parent_dir.startswith(munged_home + "-"):
            project = parent_dir[len(munged_home) + 1:]
        else:
            project = parent_dir or "general"
    except Exception:
        project = "general"

    # Extract per-turn pairs using the canonical pair parser
    try:
        pairs = _parse_jsonl_session_pairs(str(path))
    except Exception:
        return

    if not pairs:
        return

    conn = _open_fts_db()
    try:
        with conn:
            # DELETE all existing rows for this session_id (idempotent re-index)
            conn.execute(
                "DELETE FROM transcripts WHERE session_id = ?",
                (session_id,),
            )
            # INSERT one row per Q/A turn-pair
            for pair in pairs:
                user_text = pair.get("user_text", "")
                assistant_text = pair.get("assistant_text", "")
                # content is the FTS-indexed column: both sides concatenated
                content = (user_text + " " + assistant_text).strip()
                if not content:
                    continue
                conn.execute(
                    "INSERT INTO transcripts"
                    "(session_id, session_date, project, user_text, assistant_text, content)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, session_date, project, user_text, assistant_text, content),
                )
    finally:
        conn.close()


def rebuild_transcript_index() -> int:
    """Scan all session dirs and index every JSONL file. Returns count indexed."""
    count = 0
    for base_dir in SESSIONS_DIRS:
        if not base_dir.exists():
            continue
        for jsonl_path in base_dir.rglob("*.jsonl"):
            if "/subagents/" in str(jsonl_path):
                continue
            try:
                if jsonl_path.stat().st_size < 5000:
                    continue
                index_session(jsonl_path)
                count += 1
            except Exception as exc:
                log.warning("transcript_fts: failed to index session", path=str(jsonl_path), error=str(exc))
    return count


# ---------------------------------------------------------------------------
# Regex constants for text extraction
# ---------------------------------------------------------------------------

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND_TAGS_RE = re.compile(
    r"<(?:local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout)>.*?</(?:local-command-caveat|command-name|"
    r"command-message|command-args|local-command-stdout)>",
    re.DOTALL,
)


def _strip_system_noise(text: str) -> str:
    """Remove system-reminder tags and hook-injected content from text."""
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text = _COMMAND_TAGS_RE.sub("", text)
    return text.strip()


def _extract_text_only(content) -> str:
    """Extract only text blocks from content, skip tool calls and tool results."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _extract_tool_use_summary(content) -> str | None:
    """Extract a compact summary of tool_use blocks, or None if none present."""
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            tool_input = block.get("input", {})
            if name in ("Read", "Glob", "Grep"):
                parts.append(f"[Tool: {name} {tool_input.get('file_path', tool_input.get('pattern', ''))}]")
            elif name in ("Edit", "Write"):
                parts.append(f"[Tool: {name} {tool_input.get('file_path', '')}]")
            elif name == "Bash":
                parts.append(f"[Tool: Bash] {tool_input.get('command', '')[:200]}")
            else:
                parts.append(f"[Tool: {name}]")
    return "\n".join(parts) if parts else None


def parse_jsonl_session(
    path: str | Path,
    start_offset: int = 0,
) -> list[dict]:
    """Canonical JSONL session parser.

    Parses a Claude Code session JSONL file and returns a list of message dicts.
    Each dict has:
      - "role": "user" | "assistant" | "system"
      - "text": str (text content of the message, empty string if none)
      - "ts":   str | None (timestamp from the record, if present)
      - "tool_use": str | None (compact summary of tool_use blocks, if any)

    Args:
        path:         Path to the JSONL file.
        start_offset: Byte offset to seek to before reading (for incremental
                      mining). Lines before the offset are skipped. Defaults to 0
                      (read from the beginning).

    Returns:
        List of message dicts. Messages with empty text AND no tool_use are
        omitted. Role values other than "user", "assistant", and "system" are
        dropped.

    Divergence note:
        Earlier per-module parsers only kept "user" and "assistant" roles and
        dropped "system". This canonical adds "system" to the kept set because
        the schema spec explicitly lists it. Callers that previously filtered to
        user+assistant can filter on role=="system" themselves.
    """
    messages: list[dict] = []

    try:
        with open(path, "rb") as fh:
            if start_offset:
                fh.seek(start_offset)
            raw = fh.read()
    except OSError:
        return messages

    text_lines = raw.decode("utf-8", errors="ignore").splitlines()
    for line in text_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = obj.get("type", "")
        if role not in ("user", "assistant", "system"):
            continue

        content = obj.get("message", {}).get("content", "")
        text = _extract_text_only(content)
        tool_use = _extract_tool_use_summary(content)
        ts: str | None = obj.get("timestamp") or obj.get("ts") or None

        if not text and not tool_use:
            continue

        messages.append({
            "role": role,
            "text": text,
            "ts": ts,
            "tool_use": tool_use,
        })

    return messages


# ---------------------------------------------------------------------------
# Internal alias for backward-compatibility (callers inside this package used
# the old pair-returning _parse_jsonl_session; they've been updated below).
# ---------------------------------------------------------------------------

def _parse_jsonl_session_pairs(jsonl_path: str) -> list[dict]:
    """Parse JSONL session into user/assistant exchange pairs.

    LEGACY: used only by transcript_search. Standalone JSONL pair-extractor
    that includes raw content blocks (tool_result blocks in particular) needed
    by transcript_search. Cannot be replaced by parse_jsonl_session because
    the canonical only emits a tool_use summary string, not full content
    blocks. If transcript_search is ever rewritten to not need tool_result
    payloads, this can be migrated.
    """
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    pairs: list[dict] = []

    def _flush():
        if user_texts and assistant_texts:
            pairs.append({
                "user_text": "\n".join(user_texts),
                "assistant_text": "\n".join(assistant_texts),
            })
        user_texts.clear()
        assistant_texts.clear()

    def _extract_all_content(content) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""

        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block_type == "tool_use":
                name = block.get("name", "")
                tool_input = block.get("input", {})
                if name in ("Read", "Glob", "Grep"):
                    parts.append(f"[Tool: {name} {tool_input.get('file_path', tool_input.get('pattern', ''))}]")
                elif name in ("Edit", "Write"):
                    parts.append(f"[Tool: {name} {tool_input.get('file_path', '')}]")
                elif name == "Bash":
                    parts.append(f"[Tool: Bash] {tool_input.get('command', '')[:200]}")
                else:
                    parts.append(f"[Tool: {name}]")
            elif block_type == "tool_result":
                result_content = block.get("content", "")
                if block.get("is_error", False):
                    result_text = result_content if isinstance(result_content, str) else str(result_content)
                    parts.append(f"[Error] {result_text[:500]}")
                elif isinstance(result_content, str) and result_content.strip():
                    parts.append(f"[Result] {result_content[:500]}")
                elif isinstance(result_content, list):
                    for sub in result_content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            text = sub.get("text", "").strip()
                            if text:
                                parts.append(f"[Result] {text[:500]}")
        return "\n".join(parts)

    with open(jsonl_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            content = obj.get("message", {}).get("content", "")
            text = _extract_all_content(content)
            if not text:
                continue

            if msg_type == "user" and assistant_texts:
                _flush()

            if msg_type == "user":
                user_texts.append(text)
            else:
                assistant_texts.append(text)

    _flush()
    return pairs


def _extract_conversation(jsonl_path: str) -> list[str]:
    """Extract human messages and assistant prose from a session, stripped of noise.

    Returns a list of lines like "User: ..." and "Assistant: ..." with tool
    calls, tool results, and system reminders stripped out.
    """
    lines = []
    msgs = parse_jsonl_session(jsonl_path)
    for msg in msgs:
        role = msg["role"]
        text = msg["text"]
        if role == "user":
            cleaned = _strip_system_noise(text)
            if cleaned:
                lines.append(f"User: {cleaned}")
        elif role == "assistant":
            if text:
                lines.append(f"Assistant: {text}")
    return lines


def _format_results(results: list[tuple[str, dict, str]]) -> str:
    """Render a list of (score_or_rank, pair, filename) tuples as markdown.

    Shared between FTS5 and grep paths so output shape is identical.
    ``pair`` must have 'user_text' and 'assistant_text' keys.
    """
    lines = []
    for _, pair, filename in results:
        lines.append(
            f"- **Q:** {pair['user_text'][:300]}\n"
            f"  **A:** {pair['assistant_text'][:500]}\n"
            f"  *Session: {filename}*"
        )
    return "\n\n".join(lines)


def _transcript_search_fts(query: str, limit: int) -> list[tuple[float, dict, str]] | None:
    """Try FTS5 search. Returns ranked results or None on any failure.

    Returns None when the DB is missing/empty or when the query errors.
    The caller should fall back to the grep path on None.

    Each result is a per-Q/A turn-pair (one row = one exchange), mirroring
    the granularity of the grep fallback path so _format_results output is
    content-granularity-identical between both paths.
    """
    db_file = _db_path()
    if not db_file.exists():
        return None

    escaped = _fts5_escape_query(query)
    if escaped == '""':
        return None

    try:
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        try:
            # Check the DB has rows
            row_count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            if row_count == 0:
                return None

            # bm25() returns negative scores; ORDER BY ascending = best first
            # Select user_text and assistant_text directly — they are stored
            # per-pair as UNINDEXED columns, so no post-hoc splitting needed.
            rows = conn.execute(
                "SELECT session_id, user_text, assistant_text, bm25(transcripts) AS rank "
                "FROM transcripts WHERE transcripts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (escaped, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("transcript_fts: FTS5 query failed, falling back to grep", query=query, error=str(exc))
        return None

    results: list[tuple[float, dict, str]] = []
    for session_id, user_text, assistant_text, rank in rows:
        pair = {
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
        }
        filename = f"{session_id}.jsonl"
        results.append((float(rank), pair, filename))

    return results if results else None


def transcript_search(query: str, limit: int = 5) -> str:
    query_words = set(query.lower().split())
    if not query_words:
        return "No matching transcripts found"

    # --- Try FTS5 first ---
    fts_results = _transcript_search_fts(query, limit)
    if fts_results is not None:
        if not fts_results:
            return "No matching transcripts found"
        return _format_results(fts_results)

    # --- Fallback: grep path ---
    log.warning("transcript_fts: FTS5 unavailable, falling back to grep scan", query=query)

    scored = []
    files_scanned = 0
    fallback_start = time.monotonic()
    cap_hit: str | None = None

    for base_dir in SESSIONS_DIRS:
        if not base_dir.exists():
            continue
        for jsonl_path in base_dir.rglob("*.jsonl"):
            # Time-budget cap
            if time.monotonic() - fallback_start >= FALLBACK_TIME_BUDGET_S:
                cap_hit = f"time budget {FALLBACK_TIME_BUDGET_S}s exceeded"
                break
            # File-count cap
            if files_scanned >= MAX_FALLBACK_FILES:
                cap_hit = f"file count cap {MAX_FALLBACK_FILES} reached"
                break

            if "/subagents/" in str(jsonl_path):
                continue
            try:
                fsize = jsonl_path.stat().st_size
                if fsize < 5000:
                    continue
                # File-size cap: skip files larger than MAX_FALLBACK_FILE_BYTES
                if fsize > MAX_FALLBACK_FILE_BYTES:
                    continue
            except OSError:
                continue

            files_scanned += 1
            try:
                pairs = _parse_jsonl_session_pairs(str(jsonl_path))
            except Exception:
                continue
            for pair in pairs:
                text_words = set((pair["user_text"] + " " + pair["assistant_text"]).lower().split())
                score = len(query_words & text_words) / len(query_words)
                if score > 0:
                    scored.append((score, pair, jsonl_path.name))
        else:
            # inner for completed without break — check outer-loop time cap
            if time.monotonic() - fallback_start >= FALLBACK_TIME_BUDGET_S:
                cap_hit = f"time budget {FALLBACK_TIME_BUDGET_S}s exceeded"
            continue
        break  # inner loop hit a cap; stop outer loop too

    if cap_hit:
        log.warning(
            "transcript_fts: grep fallback truncated",
            reason=cap_hit,
            files_scanned=files_scanned,
            results_so_far=len(scored),
        )

    if not scored:
        return "No matching transcripts found"

    scored.sort(key=lambda item: item[0], reverse=True)
    return _format_results(scored[:limit])
