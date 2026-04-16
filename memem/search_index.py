"""SQLite FTS5 search index for memories.

Maintains a full-text search index alongside the Obsidian markdown files.
Obsidian is the source of truth, this is the query engine.
"""

import json
import logging
import re
import sqlite3

from memem.models import MEMEM_DIR, SEARCH_DB, _normalize_scope_id

log = logging.getLogger("memem-search")

_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_FTS5_TOKEN_CAP = 20

# Schema v2 adds `related_ids` column (JSON-encoded list of 8-char prefixes)
# so graph expansion can come back in the same MATCH query instead of
# requiring a follow-up _find_memory() call per FTS hit.
_FTS_SCHEMA_VERSION = 2
_FTS_COLUMNS = ("memory_id", "title", "essence", "project", "tags", "related_ids")
_CREATE_TABLE_SQL = (
    "CREATE VIRTUAL TABLE memories_fts "
    "USING fts5(memory_id, title, essence, project, tags, related_ids)"
)


def _sanitize_fts_query(query: str) -> str:
    """Tokenize user input for safe use in an FTS5 MATCH query.

    FTS5 treats bare words like AND / OR / NOT as operators and `*` / `:`
    as prefix / column syntax. Passing raw user input into MATCH can raise
    SQLite errors or silently produce wrong results. We extract alphanumeric
    tokens, drop reserved operator words, and wrap each remaining token as
    a phrase literal joined with implicit AND, capped at 20 tokens to bound
    query cost on long inputs (e.g. 200-char content snippets).
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    safe = [t for t in tokens if t.upper() not in _FTS5_OPERATORS]
    if not safe:
        return ""
    return " ".join(f'"{t}"' for t in safe[:_FTS5_TOKEN_CAP])


def _migrate_fts_schema(conn: sqlite3.Connection) -> None:
    """Drop the old table, recreate with the current schema, repopulate from vault.

    Called from `_init_search_db` when `PRAGMA user_version` is below the
    current schema version. FTS5 has no in-place ADD COLUMN, so a rebuild is
    the cleanest path. The vault is the source of truth, so no FTS data is
    actually lost — we just re-derive the index.
    """
    try:
        conn.execute("DROP TABLE IF EXISTS memories_fts")
    except sqlite3.OperationalError as exc:
        log.warning("FTS migration: drop failed: %s", exc)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(f"PRAGMA user_version = {_FTS_SCHEMA_VERSION}")
    conn.commit()
    try:
        from memem.obsidian_store import _obsidian_memories
        for mem in _obsidian_memories():
            _insert_memory_row(conn, mem)
        conn.commit()
    except ImportError:
        log.info("FTS migration: vault import unavailable; will populate lazily")
    except Exception as exc:
        log.warning("FTS migration repopulate failed: %s", exc)


def _insert_memory_row(conn: sqlite3.Connection, mem: dict) -> None:
    """Insert one memory into the FTS5 table. Used by both _index_memory
    and the migration path."""
    conn.execute(
        "INSERT INTO memories_fts "
        "(memory_id, title, essence, project, tags, related_ids) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            mem.get("id", ""),
            mem.get("title", ""),
            mem.get("essence", ""),
            mem.get("project", "general"),
            " ".join(mem.get("domain_tags", [])),
            json.dumps(mem.get("related", [])),
        ),
    )


def _init_search_db() -> sqlite3.Connection:
    """Initialize SQLite FTS5 search index. Returns connection.

    Migrates the schema from older versions on demand. FTS data is derived
    from the Obsidian vault, so the migration is safe to run at any time —
    losing the FTS table just means rebuilding it.
    """
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SEARCH_DB))
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version < _FTS_SCHEMA_VERSION:
        _migrate_fts_schema(conn)
    else:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
            "USING fts5(memory_id, title, essence, project, tags, related_ids)"
        )
        conn.commit()
    return conn


def _index_memory(mem: dict) -> None:
    """Upsert a memory into the FTS5 search index."""
    try:
        conn = _init_search_db()
        mid = mem.get("id", "")
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (mid,))
        _insert_memory_row(conn, mem)
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("FTS index failed for %s: %s", mem.get("id", "")[:8], exc)


def _remove_from_index(memory_id: str) -> None:
    """Remove a memory from the FTS5 search index."""
    try:
        conn = _init_search_db()
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _search_fts(query: str, scope_id: str = "default", limit: int = 20) -> list[str]:
    """FTS5 search, returns list of memory IDs ranked by relevance."""
    return [mid for mid, _related in _search_fts_with_graph(query, scope_id, limit)]


def _search_fts_with_graph(
    query: str, scope_id: str = "default", limit: int = 20,
) -> list[tuple[str, list[str]]]:
    """FTS5 search returning (memory_id, related_ids) tuples in one query.

    With the `related_ids` column (schema v2) the graph-expansion step no
    longer needs a follow-up lookup per hit. Callers can build the linked
    memory set directly from the second tuple element.
    """
    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return []
    conn = None
    try:
        conn = _init_search_db()
        normalized = _normalize_scope_id(scope_id)
        if normalized != "general":
            cursor = conn.execute(
                "SELECT memory_id, related_ids FROM memories_fts "
                "WHERE memories_fts MATCH ? AND project = ? ORDER BY rank LIMIT ?",
                (safe_query, normalized, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT memory_id, related_ids FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
            )
        results: list[tuple[str, list[str]]] = []
        for row in cursor.fetchall():
            try:
                related = json.loads(row[1]) if row[1] else []
            except (json.JSONDecodeError, TypeError):
                related = []
            if not isinstance(related, list):
                related = []
            results.append((row[0], related))
        return results
    except sqlite3.OperationalError as exc:
        log.warning("FTS search failed for query=%r: %s", query[:100], exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def _rebuild_search_index() -> int:
    """Rebuild the FTS5 index from all active Obsidian memories. Returns count."""
    try:
        # Lazy import to avoid circular dep (obsidian_store depends on search_index)
        from memem.obsidian_store import _obsidian_memories

        conn = _init_search_db()
        conn.execute("DELETE FROM memories_fts")
        count = 0
        for mem in _obsidian_memories():
            _insert_memory_row(conn, mem)
            count += 1
        conn.commit()
        conn.close()
        # Also rebuild the embedding side-channel if installed — strictly
        # additive so any failure here doesn't affect FTS-based search.
        try:
            from memem.embedding_index import _rebuild_embedding_index
            _rebuild_embedding_index()
        except Exception as exc:
            log.debug("embedding rebuild skipped: %s", exc)
        return count
    except Exception as exc:
        log.warning("FTS rebuild failed: %s", exc)
        return 0
