"""SQLite FTS5 search index for memories.

Maintains a full-text search index alongside the Obsidian markdown files.
Obsidian is the source of truth, this is the query engine.
"""

import logging
import sqlite3

from memem.models import MEMEM_DIR, SEARCH_DB, _normalize_scope_id

log = logging.getLogger("memem-search")


def _init_search_db() -> sqlite3.Connection:
    """Initialize SQLite FTS5 search index. Returns connection."""
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SEARCH_DB))
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
        "USING fts5(memory_id, title, essence, project, tags)"
    )
    conn.commit()
    return conn


def _index_memory(mem: dict) -> None:
    """Upsert a memory into the FTS5 search index."""
    try:
        conn = _init_search_db()
        mid = mem.get("id", "")
        conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (mid,))
        conn.execute(
            "INSERT INTO memories_fts (memory_id, title, essence, project, tags) VALUES (?, ?, ?, ?, ?)",
            (
                mid,
                mem.get("title", ""),
                mem.get("essence", ""),
                mem.get("project", "general"),
                " ".join(mem.get("domain_tags", [])),
            ),
        )
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
    try:
        conn = _init_search_db()
        normalized = _normalize_scope_id(scope_id)
        # Escape FTS5 special chars
        safe_query = query.replace('"', '""')
        if normalized != "general":
            cursor = conn.execute(
                "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? AND project = ? ORDER BY rank LIMIT ?",
                (safe_query, normalized, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
            )
        results = [row[0] for row in cursor.fetchall()]
        conn.close()
        return results
    except Exception as exc:
        log.debug("FTS search failed: %s", exc)
        return []


def _rebuild_search_index() -> int:
    """Rebuild the FTS5 index from all active Obsidian memories. Returns count."""
    try:
        # Lazy import to avoid circular dep (obsidian_store depends on search_index)
        from memem.obsidian_store import _obsidian_memories

        conn = _init_search_db()
        conn.execute("DELETE FROM memories_fts")
        count = 0
        for mem in _obsidian_memories():
            mid = mem.get("id", "")
            conn.execute(
                "INSERT INTO memories_fts (memory_id, title, essence, project, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    mid,
                    mem.get("title", ""),
                    mem.get("essence", ""),
                    mem.get("project", "general"),
                    " ".join(mem.get("domain_tags", [])),
                ),
            )
            count += 1
        conn.commit()
        conn.close()
        return count
    except Exception as exc:
        log.warning("FTS rebuild failed: %s", exc)
        return 0
