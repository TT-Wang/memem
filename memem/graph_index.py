"""Typed, scored memory graph side index.

Obsidian Markdown remains the source of truth. This SQLite database is a
rebuildable acceleration and inspection layer, analogous to search.db.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from memem.models import GRAPH_DB, MEMEM_DIR, _normalize_scope_id, now_iso

log = logging.getLogger("memem-graph")

_GRAPH_SCHEMA_VERSION = 1
_RELATION_TYPES = {
    "same_topic",
    "supports",
    "depends_on",
    "supersedes",
    "contradicts",
    "same_session",
    "same_project",
}
_NORMAL_RECALL_TYPES = {"same_topic", "supports", "depends_on", "same_session"}
_HISTORY_TYPES = _RELATION_TYPES
_MIN_EDGE_SCORE = 0.18
_HUMAN_RELATED_TYPES = {"same_topic", "supports", "depends_on", "same_session"}
_MAX_PAIR_CANDIDATES = 24
_FEATURE_CACHE: dict[tuple[str, int], dict[str, set]] = {}


def _init_graph_db() -> sqlite3.Connection:
    """Initialize graph.db and return an open sqlite connection."""
    MEMEM_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_edges (
            src_id TEXT NOT NULL,
            dst_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            score REAL NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (src_id, dst_id, relation_type)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_src ON memory_edges(src_id, score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_dst ON memory_edges(dst_id, score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_edges_type ON memory_edges(relation_type)")
    conn.execute(f"PRAGMA user_version = {_GRAPH_SCHEMA_VERSION}")
    conn.commit()
    return conn


def _id_prefix(memory_id: str) -> str:
    return (memory_id or "").strip()[:8]


def _edge_from_row(row: sqlite3.Row | tuple) -> dict[str, Any]:
    evidence = {}
    try:
        evidence = json.loads(row[4] or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {}
    return {
        "src_id": row[0],
        "dst_id": row[1],
        "relation_type": row[2],
        "score": float(row[3]),
        "evidence": evidence,
        "created_at": row[5],
        "updated_at": row[6],
    }


def _id_match_clause(column: str, memory_id: str) -> tuple[str, tuple[str, str]]:
    prefix = _id_prefix(memory_id)
    return f"({column} = ? OR substr({column}, 1, 8) = ?)", (memory_id, prefix)


def _upsert_edge(
    src_id: str,
    dst_id: str,
    relation_type: str,
    score: float,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert or update one typed edge."""
    if not src_id or not dst_id or _id_prefix(src_id) == _id_prefix(dst_id):
        return
    if relation_type not in _RELATION_TYPES:
        raise ValueError(f"Unknown relation_type: {relation_type}")
    conn = None
    try:
        conn = _init_graph_db()
        _upsert_edge_conn(conn, src_id, dst_id, relation_type, score, evidence)
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def _upsert_edge_conn(
    conn: sqlite3.Connection,
    src_id: str,
    dst_id: str,
    relation_type: str,
    score: float,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert/update one edge using an existing transaction."""
    if not src_id or not dst_id or _id_prefix(src_id) == _id_prefix(dst_id):
        return
    if relation_type not in _RELATION_TYPES:
        raise ValueError(f"Unknown relation_type: {relation_type}")
    now = now_iso()
    payload = json.dumps(evidence or {}, sort_keys=True)
    conn.execute(
        """
        INSERT INTO memory_edges
            (src_id, dst_id, relation_type, score, evidence, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(src_id, dst_id, relation_type) DO UPDATE SET
            score = excluded.score,
            evidence = excluded.evidence,
            updated_at = excluded.updated_at
        """,
        (src_id, dst_id, relation_type, float(max(0.0, min(1.0, score))), payload, now, now),
    )


def _remove_edges_for_memory(memory_id: str) -> None:
    """Remove all incoming and outgoing graph edges for a memory."""
    if not memory_id:
        return
    conn = None
    try:
        conn = _init_graph_db()
        src_clause, src_args = _id_match_clause("src_id", memory_id)
        dst_clause, dst_args = _id_match_clause("dst_id", memory_id)
        conn.execute(f"DELETE FROM memory_edges WHERE {src_clause} OR {dst_clause}", (*src_args, *dst_args))
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def _clear_graph() -> None:
    conn = None
    try:
        conn = _init_graph_db()
        conn.execute("DELETE FROM memory_edges")
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def _neighbors(
    memory_id: str,
    relation_types: set[str] | None = None,
    limit: int = 20,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Return outgoing edges for a memory, sorted by score descending."""
    if not memory_id:
        return []
    conn = None
    try:
        conn = _init_graph_db()
        clause, args = _id_match_clause("src_id", memory_id)
        params: list[Any] = [*args, min_score]
        type_sql = ""
        if relation_types:
            safe_types = [t for t in relation_types if t in _RELATION_TYPES]
            if safe_types:
                type_sql = f" AND relation_type IN ({','.join('?' for _ in safe_types)})"
                params.extend(safe_types)
        params.append(limit)
        cur = conn.execute(
            f"""
            SELECT src_id, dst_id, relation_type, score, evidence, created_at, updated_at
            FROM memory_edges
            WHERE {clause} AND score >= ?{type_sql}
            ORDER BY score DESC, updated_at DESC
            LIMIT ?
            """,
            params,
        )
        return [_edge_from_row(row) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        log.debug("graph neighbor lookup failed: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def _reverse_neighbors(
    memory_id: str,
    relation_types: set[str] | None = None,
    limit: int = 20,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Return incoming edges for a memory, sorted by score descending."""
    if not memory_id:
        return []
    conn = None
    try:
        conn = _init_graph_db()
        clause, args = _id_match_clause("dst_id", memory_id)
        params: list[Any] = [*args, min_score]
        type_sql = ""
        if relation_types:
            safe_types = [t for t in relation_types if t in _RELATION_TYPES]
            if safe_types:
                type_sql = f" AND relation_type IN ({','.join('?' for _ in safe_types)})"
                params.extend(safe_types)
        params.append(limit)
        cur = conn.execute(
            f"""
            SELECT src_id, dst_id, relation_type, score, evidence, created_at, updated_at
            FROM memory_edges
            WHERE {clause} AND score >= ?{type_sql}
            ORDER BY score DESC, updated_at DESC
            LIMIT ?
            """,
            params,
        )
        return [_edge_from_row(row) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        log.debug("graph reverse-neighbor lookup failed: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def _safe_int(value: Any, default: int = 3) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _memory_text(mem: dict) -> str:
    return f"{mem.get('title', '')} {mem.get('essence') or mem.get('full_record', '')}"


def _raw_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9_]{3,}", text)}


def _scoring_features(mem: dict) -> dict[str, set]:
    """Cached expensive lexical features for pair scoring."""
    from memem.obsidian_store import _ngram_set, _word_set

    text = _memory_text(mem)
    key = (mem.get("id", ""), hash(text))
    cached = _FEATURE_CACHE.get(key)
    if cached is not None:
        return cached
    features = {
        "words": _word_set(text),
        "bigrams": _ngram_set(text, 2),
        "trigrams": _ngram_set(text, 3),
    }
    _FEATURE_CACHE[key] = features
    return features


def _score_memory_pair(src: dict, dst: dict) -> dict[str, Any]:
    """Compute deterministic relation features and a combined edge score."""
    from memem.obsidian_store import _containment

    src_features = _scoring_features(src)
    dst_features = _scoring_features(dst)
    src_words = src_features["words"]
    dst_words = dst_features["words"]
    if not src_words or not dst_words:
        lexical = 0.0
    else:
        word_c = max(_containment(src_words, dst_words), _containment(dst_words, src_words))
        bigram_c = max(
            _containment(src_features["bigrams"], dst_features["bigrams"]),
            _containment(dst_features["bigrams"], src_features["bigrams"]),
        )
        trigram_c = max(
            _containment(src_features["trigrams"], dst_features["trigrams"]),
            _containment(dst_features["trigrams"], src_features["trigrams"]),
        )
        lexical = 0.5 * word_c + 0.3 * bigram_c + 0.2 * trigram_c

    src_tags = {str(t).lower() for t in src.get("domain_tags", []) if t}
    dst_tags = {str(t).lower() for t in dst.get("domain_tags", []) if t}
    tag_score = len(src_tags & dst_tags) / max(len(src_tags | dst_tags), 1) if (src_tags or dst_tags) else 0.0

    src_project = _normalize_scope_id(src.get("project", "general"))
    dst_project = _normalize_scope_id(dst.get("project", "general"))
    project_score = 1.0 if src_project == dst_project and src_project != "general" else 0.0

    src_session = src.get("source_session", "")
    dst_session = dst.get("source_session", "")
    session_score = 1.0 if src_session and src_session == dst_session else 0.0

    src_layer = _safe_int(src.get("layer"), 2)
    dst_layer = _safe_int(dst.get("layer"), 2)
    layer_score = max(0.0, 1.0 - abs(src_layer - dst_layer) / 3.0)
    importance_score = (_safe_int(src.get("importance"), 3) + _safe_int(dst.get("importance"), 3)) / 10.0
    structural_score = 0.5 * layer_score + 0.5 * importance_score

    dst_prefix = _id_prefix(dst.get("id", ""))
    explicit_related = 1.0 if dst_prefix in {_id_prefix(r) for r in src.get("related", [])} else 0.0
    explicit_contradiction = 1.0 if dst_prefix in {_id_prefix(r) for r in src.get("contradicts", [])} else 0.0

    score = (
        0.45 * lexical
        + 0.20 * tag_score
        + 0.15 * project_score
        + 0.10 * session_score
        + 0.05 * structural_score
        + 0.05 * max(explicit_related, explicit_contradiction)
    )

    return {
        "score": max(0.0, min(1.0, score)),
        "lexical": lexical,
        "tag_score": tag_score,
        "project_score": project_score,
        "session_score": session_score,
        "structural_score": structural_score,
        "explicit_related": explicit_related,
        "explicit_contradiction": explicit_contradiction,
    }


def _classify_relation(src: dict, dst: dict, features: dict[str, Any]) -> str:
    """Classify an edge using a deliberately small relation vocabulary."""
    dst_prefix = _id_prefix(dst.get("id", ""))
    if features.get("explicit_contradiction") or dst_prefix in {_id_prefix(r) for r in src.get("contradicts", [])}:
        return "contradicts"

    src_tags = [str(t).lower() for t in src.get("domain_tags", []) if t]
    if any(t.startswith("supersedes:") for t in src_tags) and features.get("lexical", 0.0) >= 0.12:
        return "supersedes"

    text = _memory_text(src).lower()
    if features.get("lexical", 0.0) >= 0.14 and any(
        signal in text for signal in ("depends on", "requires", "prerequisite", "must run before", "needs ")
    ):
        return "depends_on"

    if features.get("lexical", 0.0) >= 0.25:
        return "same_topic"
    if features.get("tag_score", 0.0) >= 0.25 and features.get("lexical", 0.0) >= 0.12:
        return "supports"
    if features.get("session_score", 0.0) >= 1.0:
        return "same_session"
    return "same_project"


def _is_active(mem: dict) -> bool:
    return mem.get("status", "active") != "deprecated"


def _candidate_pool_for_memory(mem: dict, memories: list[dict], max_candidates: int) -> list[dict]:
    """Cheap prefilter before expensive word/bigram/trigram scoring.

    Full graph rebuild can otherwise become O(N²) with n-gram construction
    inside each pair comparison. This stage uses raw token overlap and
    metadata signals to keep the expensive scorer bounded per memory.
    """
    src_id = mem.get("id", "")
    src_prefix = _id_prefix(src_id)
    src_words = _raw_words(_memory_text(mem))
    src_tags = {str(t).lower() for t in mem.get("domain_tags", []) if t}
    src_project = _normalize_scope_id(mem.get("project", "general"))
    src_session = mem.get("source_session", "")
    explicit_ids = {_id_prefix(r) for r in mem.get("related", [])} | {_id_prefix(r) for r in mem.get("contradicts", [])}

    scored: list[tuple[float, dict]] = []
    for other in memories:
        dst_id = other.get("id", "")
        dst_prefix = _id_prefix(dst_id)
        if not src_id or not dst_id or src_prefix == dst_prefix or not _is_active(other):
            continue

        score = 0.0
        if dst_prefix in explicit_ids:
            score += 1.0
        dst_session = other.get("source_session", "")
        if src_session and src_session == dst_session:
            score += 0.7
        dst_tags = {str(t).lower() for t in other.get("domain_tags", []) if t}
        if src_tags and dst_tags:
            score += 0.4 * (len(src_tags & dst_tags) / max(len(src_tags | dst_tags), 1))
        dst_project = _normalize_scope_id(other.get("project", "general"))
        if src_project == dst_project and src_project != "general":
            score += 0.15
        dst_words = _raw_words(_memory_text(other))
        if src_words and dst_words:
            overlap = len(src_words & dst_words) / max(min(len(src_words), len(dst_words)), 1)
            score += overlap

        if score >= 0.08:
            scored.append((score, other))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [other for _score, other in scored[:max_candidates]]


def _build_candidate_index(memories: list[dict]) -> dict[str, Any]:
    """Build inverted indexes for bounded graph rebuild candidate lookup."""
    by_prefix: dict[str, dict] = {}
    words: dict[str, list[str]] = defaultdict(list)
    tags: dict[str, list[str]] = defaultdict(list)
    sessions: dict[str, list[str]] = defaultdict(list)
    for mem in memories:
        mid = _id_prefix(mem.get("id", ""))
        if not mid or not _is_active(mem):
            continue
        by_prefix[mid] = mem
        for word in _raw_words(_memory_text(mem)):
            words[word].append(mid)
        for tag in {str(t).lower() for t in mem.get("domain_tags", []) if t}:
            tags[tag].append(mid)
        session = mem.get("source_session", "")
        if session:
            sessions[session].append(mid)
    return {"by_prefix": by_prefix, "words": words, "tags": tags, "sessions": sessions}


def _candidate_pool_from_index(mem: dict, candidate_index: dict[str, Any], max_candidates: int) -> list[dict]:
    """Return top candidate memories using inverted indexes, not full scans."""
    src_prefix = _id_prefix(mem.get("id", ""))
    by_prefix: dict[str, dict] = candidate_index["by_prefix"]
    scores: Counter[str] = Counter()

    for rid in mem.get("related", []) or []:
        prefix = _id_prefix(rid)
        if prefix and prefix != src_prefix:
            scores[prefix] += 30
    for rid in mem.get("contradicts", []) or []:
        prefix = _id_prefix(rid)
        if prefix and prefix != src_prefix:
            scores[prefix] += 30

    for tag in {str(t).lower() for t in mem.get("domain_tags", []) if t}:
        for mid in candidate_index["tags"].get(tag, []):
            if mid != src_prefix:
                scores[mid] += 6

    session = mem.get("source_session", "")
    if session:
        for mid in candidate_index["sessions"].get(session, []):
            if mid != src_prefix:
                scores[mid] += 8

    for word in _raw_words(_memory_text(mem)):
        postings = candidate_index["words"].get(word, [])
        # Extremely common words create noisy, expensive candidate sets.
        if len(postings) > 250:
            continue
        for mid in postings:
            if mid != src_prefix:
                scores[mid] += 1

    return [
        by_prefix[mid]
        for mid, _score in scores.most_common(max_candidates)
        if mid in by_prefix
    ]


def _edge_candidates_for_memory(
    mem: dict,
    memories: list[dict],
    limit: int = 8,
    candidate_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return top edge candidates for one memory without writing graph.db."""
    src_id = mem.get("id", "")
    scored: list[dict[str, Any]] = []
    max_candidates = max(_MAX_PAIR_CANDIDATES, limit * 8)
    pool = (
        _candidate_pool_from_index(mem, candidate_index, max_candidates)
        if candidate_index is not None
        else _candidate_pool_for_memory(mem, memories, max_candidates)
    )
    for other in pool:
        dst_id = other.get("id", "")
        if not src_id or not dst_id or _id_prefix(src_id) == _id_prefix(dst_id):
            continue
        if not _is_active(other):
            continue
        features = _score_memory_pair(mem, other)
        relation_type = _classify_relation(mem, other, features)
        score = float(features["score"])
        if relation_type in {"contradicts", "supersedes"}:
            score = max(score, 0.80)
        elif (relation_type == "same_project" and score < 0.30) or score < _MIN_EDGE_SCORE:
            continue
        scored.append({
            "src_id": src_id,
            "dst_id": dst_id,
            "relation_type": relation_type,
            "score": score,
            "evidence": features,
        })
    scored.sort(key=lambda e: (e["score"], e["relation_type"]), reverse=True)
    return scored[:limit]


def _suggest_related(mem: dict, memories: list[dict] | None = None, limit: int = 3) -> list[str]:
    """Return human-facing related ID prefixes using the new scorer."""
    if memories is None:
        from memem.obsidian_store import _obsidian_memories
        memories = _obsidian_memories(None)
    edges = [
        e for e in _edge_candidates_for_memory(mem, memories, limit=max(limit * 3, 8))
        if e["relation_type"] in _HUMAN_RELATED_TYPES
    ]
    return [_id_prefix(e["dst_id"]) for e in edges[:limit]]


def _refresh_edges_for_memory(mem: dict, limit: int = 8) -> int:
    """Recompute outgoing edges for one memory and insert reciprocal symmetric edges."""
    if not mem.get("id") or not _is_active(mem):
        _remove_edges_for_memory(mem.get("id", ""))
        return 0
    from memem.obsidian_store import _obsidian_memories

    memories = _obsidian_memories(None)
    # The memory's topic, status, or metadata may have changed, so all edges
    # touching it are stale until recomputed.
    _remove_edges_for_memory(mem.get("id", ""))
    count = 0
    for edge in _edge_candidates_for_memory(mem, memories, limit=limit):
        _upsert_edge(edge["src_id"], edge["dst_id"], edge["relation_type"], edge["score"], edge["evidence"])
        count += 1
        if edge["relation_type"] in {"same_topic", "same_session", "same_project"}:
            _upsert_edge(edge["dst_id"], edge["src_id"], edge["relation_type"], edge["score"], edge["evidence"])
            count += 1
    return count


def _rebuild_graph(scope_id: str | None = None, limit_per_memory: int = 8) -> int:
    """Rebuild graph.db from active Obsidian memories. Returns edge count."""
    from memem.obsidian_store import _obsidian_memories

    scope_arg = None if not scope_id or _normalize_scope_id(scope_id) == "general" else scope_id
    memories = [m for m in _obsidian_memories(scope_arg) if _is_active(m)]
    _FEATURE_CACHE.clear()
    _clear_graph()
    candidate_index = _build_candidate_index(memories)
    count = 0
    conn = None
    try:
        conn = _init_graph_db()
        for mem in memories:
            for edge in _edge_candidates_for_memory(
                mem, memories, limit=limit_per_memory, candidate_index=candidate_index
            ):
                _upsert_edge_conn(
                    conn,
                    edge["src_id"],
                    edge["dst_id"],
                    edge["relation_type"],
                    edge["score"],
                    edge["evidence"],
                )
                count += 1
                if edge["relation_type"] in {"same_topic", "same_session", "same_project"}:
                    _upsert_edge_conn(
                        conn,
                        edge["dst_id"],
                        edge["src_id"],
                        edge["relation_type"],
                        edge["score"],
                        edge["evidence"],
                    )
                    count += 1
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return count


def _graph_stats() -> dict[str, Any]:
    conn = None
    try:
        conn = _init_graph_db()
        total = conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        by_type = {
            row[0]: row[1]
            for row in conn.execute("SELECT relation_type, COUNT(*) FROM memory_edges GROUP BY relation_type")
        }
        return {"edges": total, "by_type": by_type, "db": str(GRAPH_DB)}
    finally:
        if conn is not None:
            conn.close()


def _audit_graph() -> dict[str, Any]:
    """Return graph quality diagnostics."""
    from memem.obsidian_store import _find_memory, _obsidian_memories

    mems = _obsidian_memories(None, include_deprecated=True)
    active_ids = {m.get("id", "") for m in mems if _is_active(m)}
    active_prefixes = {_id_prefix(mid) for mid in active_ids}
    deprecated_prefixes = {_id_prefix(m.get("id", "")) for m in mems if not _is_active(m)}
    stats = _graph_stats()

    conn = None
    edges: list[dict[str, Any]] = []
    try:
        conn = _init_graph_db()
        cur = conn.execute(
            "SELECT src_id, dst_id, relation_type, score, evidence, created_at, updated_at FROM memory_edges"
        )
        edges = [_edge_from_row(row) for row in cur.fetchall()]
    finally:
        if conn is not None:
            conn.close()

    linked_src = {_id_prefix(e["src_id"]) for e in edges}
    linked_dst = {_id_prefix(e["dst_id"]) for e in edges}
    linked = linked_src | linked_dst
    dead_links = [
        {"src_id": e["src_id"][:8], "dst_id": e["dst_id"][:8], "relation_type": e["relation_type"]}
        for e in edges
        if _id_prefix(e["src_id"]) not in active_prefixes or _id_prefix(e["dst_id"]) not in active_prefixes
    ]
    deprecated_linked = [
        {"src_id": e["src_id"][:8], "dst_id": e["dst_id"][:8], "relation_type": e["relation_type"]}
        for e in edges
        if _id_prefix(e["src_id"]) in deprecated_prefixes or _id_prefix(e["dst_id"]) in deprecated_prefixes
    ]
    one_way = []
    edge_pairs = {(_id_prefix(e["src_id"]), _id_prefix(e["dst_id"]), e["relation_type"]) for e in edges}
    for src, dst, rel in edge_pairs:
        if rel in {"same_topic", "same_session", "same_project"} and (dst, src, rel) not in edge_pairs:
            one_way.append({"src_id": src, "dst_id": dst, "relation_type": rel})
    degree: dict[str, int] = {}
    for e in edges:
        degree[_id_prefix(e["src_id"])] = degree.get(_id_prefix(e["src_id"]), 0) + 1
        degree[_id_prefix(e["dst_id"])] = degree.get(_id_prefix(e["dst_id"]), 0) + 1
    overlinked = [
        {"memory_id": mid, "degree": deg}
        for mid, deg in sorted(degree.items(), key=lambda item: item[1], reverse=True)
        if deg >= 20 and _find_memory(mid)
    ]
    low_confidence = [
        {"src_id": e["src_id"][:8], "dst_id": e["dst_id"][:8], "score": round(e["score"], 3)}
        for e in edges
        if e["score"] < _MIN_EDGE_SCORE
    ]
    return {
        **stats,
        "active_memories": len(active_ids),
        "orphan_memories": sorted(active_prefixes - linked),
        "dead_links": dead_links,
        "one_way_links": one_way,
        "overlinked_hubs": overlinked,
        "deprecated_linked": deprecated_linked,
        "low_confidence_edges": low_confidence,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def format_graph_neighbors(memory_id: str, include_history: bool = False, limit: int = 20) -> str:
    """Human-readable neighbor report for CLI/MCP."""
    relation_types = _HISTORY_TYPES if include_history else _NORMAL_RECALL_TYPES
    edges = _neighbors(memory_id, relation_types=relation_types, limit=limit)
    if not edges:
        return f"No graph neighbors found for: {memory_id}"
    from memem.obsidian_store import _find_memory

    lines = [f"### Graph neighbors for {memory_id[:8]}"]
    for edge in edges:
        mem = _find_memory(edge["dst_id"])
        title = mem.get("title", "Untitled") if mem else "[missing]"
        lines.append(
            f"- [{edge['dst_id'][:8]}] {edge['relation_type']} "
            f"{edge['score']:.2f} — {title}"
        )
    return "\n".join(lines)


def format_graph_audit() -> str:
    audit = _audit_graph()
    lines = [
        "### memem graph audit",
        f"- edges: {audit['edges']}",
        f"- active memories: {audit['active_memories']}",
        f"- by type: {json.dumps(audit['by_type'], sort_keys=True)}",
        f"- orphan memories: {len(audit['orphan_memories'])}",
        f"- dead links: {len(audit['dead_links'])}",
        f"- one-way links: {len(audit['one_way_links'])}",
        f"- deprecated linked: {len(audit['deprecated_linked'])}",
        f"- low-confidence edges: {len(audit['low_confidence_edges'])}",
    ]
    if audit["overlinked_hubs"]:
        hubs = ", ".join(f"{h['memory_id']}({h['degree']})" for h in audit["overlinked_hubs"][:10])
        lines.append(f"- overlinked hubs: {hubs}")
    return "\n".join(lines)
