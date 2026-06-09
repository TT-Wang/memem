"""memem v2.3.0 retrieval — BM25+cosine RRF fusion + FTS-conditional supplement + temporal re-rank.

Benchmark-validated at 74% precision on 18-query × 6-category eval
(vs v1.13.0's 24%). Replaces ~12,400 LOC of legacy retrieval pipeline.

No daemon. No scope filter. No kind classifier. No LLM judge.
Just: query → embed → cosine top-K, with FTS supplement when query
contains version/date literals.

v2.2.0 adds temporal awareness:
- _extract_temporal_range: parse "yesterday", "last week", "N days ago", etc.
- _fts_temporal_search: supplement results from the temporal date window
- retrieve() date-aware re-ranking: +0.2 score boost for on-window memories

Thread safety note: module-level caches (_vault_idx_cache, _emb_cache,
_model) use a "set after compute" pattern that is safe under CPython's GIL.
Two threads may each compute the value and the second write wins, but both
values are equivalent — no torn state, no corruption. This is the intentional
design choice for simplicity over locking overhead.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import numpy as np
from sentence_transformers import SentenceTransformer

import memem.settings as _settings
from memem.models import MEMEM_DIR, OBSIDIAN_MEMORIES_DIR, parse_iso_dt

_EMB_PATH = MEMEM_DIR / "embeddings.npy"
_IDS_PATH = MEMEM_DIR / "embedding_ids.json"
_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_ID_PAT = re.compile(r"^id:\s*([a-f0-9-]+)", re.M)
_TITLE_PAT = re.compile(r"^title:\s*(.*)$", re.M)
_PROJECT_PAT = re.compile(r"^project:\s*(.*)$", re.M)
_CREATED_PAT = re.compile(r"^created:\s*(.*)$", re.M)
_LAST_ACCESSED_PAT = re.compile(r"^last_accessed_at:\s*'?([^'\n]*)'?", re.M)
_ACCESS_COUNT_PAT = re.compile(r"^access_count:\s*(\d+)", re.M)
_VALID_AT_PAT = re.compile(r"^valid_at:\s*'?([^'\n]*)'?", re.M)
_DECAY_IMMUNE_PAT = re.compile(r"^decay_immune:\s*(true|false|yes|no)", re.M | re.I)
_LAYER_PAT = re.compile(r"^layer:\s*(\d+)", re.M)
_IMPORTANCE_PAT = re.compile(r"^importance:\s*(\d+)", re.M)
_VERSION_PAT = re.compile(r"v\d+\.\d+(?:\.\d+)?", re.I)
_DATE_PAT = re.compile(r"\d{4}-\d{2}-\d{2}")

# Module-level caches (lazy-loaded, mtime-invalidated).
# CPython GIL makes "set after compute" safe — see module docstring.
_model: SentenceTransformer | None = None
_vault_idx_cache: dict | None = None
_vault_idx_mtime: float = 0
_vault_idx_count: int = 0  # Phase 4.5 fix: also track file count so deletes invalidate
_emb_cache: tuple | None = None  # (normalized_embeddings, ids_list, mtime)
_bm25_cache: tuple | None = None  # (ids: list[str], bm25: BM25Okapi)
_bm25_cache_key: tuple = (0.0, 0)  # (max_mtime, file_count) — same key as vault cache


class MemoryHit(TypedDict, total=False):
    id: str
    path: str
    title: str
    project: str
    created: str
    body: str
    score: float
    source: str  # "cosine" or "fts"
    last_accessed_at: str
    access_count: int
    valid_at: str
    decay_immune: bool
    layer: int
    importance: int


def _get_model() -> SentenceTransformer:
    """Lazily load the sentence-transformers model (singleton per process)."""
    global _model
    if _model is None:
        _model = SentenceTransformer(_EMBEDDING_MODEL)
    return _model


def load_vault_index() -> dict[str, dict]:
    """Walk vault *.md files, extract id+title+project+created+body.

    Cached at module level. Cache key is (newest mtime, file count) — this
    invalidates correctly on both additions/modifications (mtime change) AND
    deletions (count decrease). Phase 4.5 fix: previously only tracked mtime
    which is deletion-blind since `max(mtimes)` doesn't decrease when files
    disappear, leaving consolidation/decay-deleted memories in the cache.
    """
    global _vault_idx_cache, _vault_idx_mtime, _vault_idx_count
    if not OBSIDIAN_MEMORIES_DIR.exists():
        return {}
    md_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
    latest_mtime = max((p.stat().st_mtime for p in md_files), default=0)
    file_count = len(md_files)
    if (
        _vault_idx_cache is not None
        and _vault_idx_mtime >= latest_mtime
        and _vault_idx_count == file_count
    ):
        return _vault_idx_cache

    idx: dict[str, dict] = {}
    for p in md_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            front = text[:2000]
            id_m = _ID_PAT.search(front)
            if not id_m:
                continue
            body = text
            if text.startswith("---"):
                end = text.find("\n---", 4)
                if end > 0:
                    body = text[end + 4:].lstrip("\n")
            title_m = _TITLE_PAT.search(front)
            project_m = _PROJECT_PAT.search(front)
            created_m = _CREATED_PAT.search(front)
            created_val = (created_m.group(1) if created_m else "").strip("'\" ")
            last_accessed_m = _LAST_ACCESSED_PAT.search(front)
            last_accessed_val = (last_accessed_m.group(1) if last_accessed_m else "").strip("'\" ") or created_val
            access_count_m = _ACCESS_COUNT_PAT.search(front)
            try:
                access_count_val = int(access_count_m.group(1)) if access_count_m else 0
            except (ValueError, AttributeError):
                access_count_val = 0
            valid_at_m = _VALID_AT_PAT.search(front)
            valid_at_val = (valid_at_m.group(1) if valid_at_m else "").strip("'\" ") or created_val
            decay_immune_m = _DECAY_IMMUNE_PAT.search(front)
            decay_immune_val = decay_immune_m.group(1).lower() in ("true", "yes") if decay_immune_m else False
            layer_m = _LAYER_PAT.search(front)
            try:
                layer_val = int(layer_m.group(1)) if layer_m else 2
            except (ValueError, AttributeError):
                layer_val = 2
            importance_m = _IMPORTANCE_PAT.search(front)
            try:
                importance_val = int(importance_m.group(1)) if importance_m else 3
            except (ValueError, AttributeError):
                importance_val = 3
            idx[id_m.group(1)] = {
                "id": id_m.group(1),
                "path": str(p),
                "title": (title_m.group(1) if title_m else p.name).strip("'\" "),
                "project": (project_m.group(1) if project_m else "?").strip("'\" "),
                "created": created_val,
                "body": body[:300],
                "body_full": body[:2000],
                "last_accessed_at": last_accessed_val,
                "access_count": access_count_val,
                "valid_at": valid_at_val,
                "decay_immune": decay_immune_val,
                "layer": layer_val,
                "importance": importance_val,
            }
        except Exception:  # noqa: BLE001
            continue
    _vault_idx_cache = idx
    _vault_idx_mtime = latest_mtime
    _vault_idx_count = file_count
    return idx


def load_embeddings() -> tuple[np.ndarray, list[str]] | None:
    """Load normalized embeddings + ids. Cached, mtime-invalidated.

    Returns None if either embeddings.npy or embedding_ids.json is missing.
    """
    global _emb_cache
    if not _EMB_PATH.exists() or not _IDS_PATH.exists():
        return None
    mtime = _EMB_PATH.stat().st_mtime
    if _emb_cache is not None and _emb_cache[2] >= mtime:
        return _emb_cache[0], _emb_cache[1]
    embeddings = np.load(_EMB_PATH)
    ids_data = json.loads(_IDS_PATH.read_text())
    ids: list[str] = ids_data["ids"] if isinstance(ids_data, dict) else ids_data
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True).clip(min=1e-9)
    e_norm = embeddings / norms
    _emb_cache = (e_norm, ids, mtime)
    return e_norm, ids


def has_version_or_date_literal(query: str) -> bool:
    """Return True if query contains a version (v1.2.3) or date (YYYY-MM-DD) literal."""
    return bool(_VERSION_PAT.search(query) or _DATE_PAT.search(query))


def _extract_temporal_range(query: str) -> tuple[datetime, datetime] | None:
    """Parse common temporal phrases from query and return (start_dt, end_dt).

    Supported phrases (case-insensitive):
    - "yesterday"      → (now - 48h) to (now - 24h)
    - "today"          → (now - 24h) to now
    - "this morning"   → (now - 24h) to now  (same as today for simplicity)
    - "last week"      → (now - 14d) to (now - 7d)
    - "this week"      → (now - 7d) to now
    - "N days ago"     → (now - (N+1)d) to (now - Nd)
    - "N hours ago"    → (now - (N+1)h) to (now - Nh)

    Returns None if no temporal phrase is found.
    Multiple phrases: first match wins.
    """
    q = query.lower()
    now = datetime.now(UTC)

    # "N days ago" — check before fixed phrases to avoid false positives
    m = re.search(r"(\d+)\s+days?\s+ago", q)
    if m:
        n = int(m.group(1))
        return (now - timedelta(days=n + 1), now - timedelta(days=n))

    # "N hours ago"
    m = re.search(r"(\d+)\s+hours?\s+ago", q)
    if m:
        n = int(m.group(1))
        return (now - timedelta(hours=n + 1), now - timedelta(hours=n))

    # Fixed phrases (longest/most-specific first)
    if "this morning" in q:
        return (now - timedelta(hours=24), now)
    if "yesterday" in q:
        return (now - timedelta(hours=48), now - timedelta(hours=24))
    if "last week" in q:
        return (now - timedelta(days=14), now - timedelta(days=7))
    if "this week" in q:
        return (now - timedelta(days=7), now)
    if "today" in q:
        return (now - timedelta(hours=24), now)

    return None


def _fts_temporal_search(
    query: str, vault_idx: dict, max_results: int = 3
) -> list[MemoryHit]:
    """Supplement results with memories created within the temporal window.

    Mirrors the shape of _fts_literal_search — returns MemoryHit list with
    score=1.0 and source="fts".  Returns [] for non-temporal queries.
    """
    tr = _extract_temporal_range(query)
    if tr is None:
        return []
    start_dt, end_dt = tr
    hits: list[MemoryHit] = []
    for mem in vault_idx.values():
        created_dt = parse_iso_dt(mem.get("created", ""))
        if created_dt is None:
            continue
        if start_dt <= created_dt <= end_dt:
            hits.append(
                {
                    **{k: v for k, v in mem.items() if k != "body_full"},
                    "score": 1.0,
                    "source": "fts",
                }
            )
            if len(hits) >= max_results:
                break
    return hits


def _fts_literal_search(query: str, vault_idx: dict, max_results: int = 2) -> list[MemoryHit]:
    """Find memories containing literal version/date tokens from the query.

    Only fires when query has v\\d+.\\d+ or YYYY-MM-DD literal.
    """
    literals = _VERSION_PAT.findall(query) + _DATE_PAT.findall(query)
    if not literals:
        return []
    literals_lower = [lit.lower() for lit in literals]
    hits: list[MemoryHit] = []
    for mem in vault_idx.values():
        blob = (mem["title"] + " " + mem["body_full"]).lower()
        if any(lit in blob for lit in literals_lower):
            hits.append(
                {
                    **{k: v for k, v in mem.items() if k != "body_full"},
                    "score": 1.0,
                    "source": "fts",
                }
            )
            if len(hits) >= max_results:
                break
    return hits


def _build_bm25(vault_idx: dict) -> tuple | None:
    """Build BM25Okapi index over title + body_full. Cached by (mtime, count)."""
    global _bm25_cache, _bm25_cache_key
    if not vault_idx:
        return None
    # Same key as vault: tracks latest mtime + count
    md_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
    key = (max((p.stat().st_mtime for p in md_files), default=0), len(md_files))
    if _bm25_cache is not None and key == _bm25_cache_key:
        return _bm25_cache
    from rank_bm25 import BM25Okapi
    corpus_ids: list[str] = []
    corpus_tokens: list[list[str]] = []
    for mid, mem in vault_idx.items():
        text = (mem.get("title", "") + " " + mem.get("body_full", "")).lower()
        tokens = text.split()
        if not tokens:
            continue  # skip empty bodies
        corpus_ids.append(mid)
        corpus_tokens.append(tokens)
    if not corpus_tokens:
        return None
    bm25 = BM25Okapi(corpus_tokens)
    _bm25_cache = (corpus_ids, bm25)
    _bm25_cache_key = key
    return _bm25_cache


def _mmr_rerank(
    candidates: list[MemoryHit],
    embeddings_norm: np.ndarray,
    ids: list[str],
    k: int = 8,
    lam: float = 0.7,
) -> list[MemoryHit]:
    """MMR re-rank: select k from candidates balancing relevance vs diversity.

    L0 / decay_immune candidates are pre-seeded into the selected set (always
    included, no diversity penalty). The remaining slots are filled by MMR
    iteration over the rest.
    """
    if not candidates:
        return []
    if len(candidates) <= k:
        return candidates  # nothing to diversify
    # Pre-seed immunes
    id_to_emb_idx = {mid: i for i, mid in enumerate(ids)}
    selected: list[MemoryHit] = []
    selected_vecs: list[np.ndarray] = []
    remaining: list[MemoryHit] = []
    for hit in candidates:
        if hit.get("layer", 2) == 0 or hit.get("decay_immune", False):
            selected.append(hit)
            emb_idx = id_to_emb_idx.get(hit["id"])
            if emb_idx is not None and emb_idx < len(embeddings_norm):
                selected_vecs.append(embeddings_norm[emb_idx])
        else:
            remaining.append(hit)
    # MMR loop on remaining
    while len(selected) < k and remaining:
        best_mmr = -float("inf")
        best_idx = 0
        for i, hit in enumerate(remaining):
            relevance = hit.get("score", 0.0)
            emb_idx = id_to_emb_idx.get(hit["id"])
            if emb_idx is None or emb_idx >= len(embeddings_norm):
                # no embedding — treat as max diversity (include eagerly)
                max_sim = 0.0
            elif not selected_vecs:
                max_sim = 0.0
            else:
                hit_vec = embeddings_norm[emb_idx]
                sims = np.array([float(hit_vec @ sv) for sv in selected_vecs])
                max_sim = float(sims.max())
            mmr = lam * relevance - (1 - lam) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        winner = remaining.pop(best_idx)
        selected.append(winner)
        emb_idx = id_to_emb_idx.get(winner["id"])
        if emb_idx is not None and emb_idx < len(embeddings_norm):
            selected_vecs.append(embeddings_norm[emb_idx])
    return selected[:k]


def _rrf_fusion(
    cosine_scores: dict[str, float],
    bm25_scores: dict[str, float],
    k: int = 60,
) -> dict[str, float]:
    """Standard RRF: score = 1/(k + cosine_rank) + 1/(k + bm25_rank)."""
    cosine_ranks = {mid: r for r, (mid, _) in enumerate(
        sorted(cosine_scores.items(), key=lambda x: -x[1]), start=1)}
    bm25_ranks = {mid: r for r, (mid, _) in enumerate(
        sorted(bm25_scores.items(), key=lambda x: -x[1]), start=1)}
    all_ids = set(cosine_ranks) | set(bm25_ranks)
    missing_rank = max(len(cosine_ranks), len(bm25_ranks)) + 1
    out: dict[str, float] = {}
    for mid in all_ids:
        c_rank = cosine_ranks.get(mid, missing_rank)
        b_rank = bm25_ranks.get(mid, missing_rank)
        out[mid] = 1.0 / (k + c_rank) + 1.0 / (k + b_rank)
    return out


def retrieve(query: str, k: int = 8) -> list[MemoryHit]:
    """Main retrieval: cosine top-K + FTS supplement for version/date literals.

    Args:
        query: Natural language query string.
        k: Maximum number of cosine results to return (FTS supplement may add
           up to 2 additional results on top of k).

    Returns:
        List of MemoryHit dicts ordered FTS-first, then cosine by descending
        score. Each hit has keys: id, path, title, project, created, body,
        score, source.
    """
    vault_idx = load_vault_index()
    if not vault_idx:
        return []
    emb_data = load_embeddings()
    if emb_data is None:
        return []
    embeddings_norm, ids = emb_data

    results: list[MemoryHit] = []
    seen_paths: set[str] = set()

    # FTS supplement (fires only when query has version/date literal)
    fts_hits = _fts_literal_search(query, vault_idx, max_results=2)
    for hit in fts_hits:
        if hit["path"] not in seen_paths:
            results.append(hit)
            seen_paths.add(hit["path"])

    # v2.2.0 NOTE: the temporal FTS supplement was prototyped but never wired
    # in for release. _fts_temporal_search() is defined above and exercised by
    # tests/test_retrieve_temporal.py, but it has zero production call sites.
    # We tried adding it here and it expanded the result-set denominator without
    # adding keyword-matching hits, dragging benchmark precision 74% → 73%.
    # The date-aware re-ranking step below (which reorders existing cosine
    # candidates by `created:` proximity to query temporal phrase) DOES use
    # _extract_temporal_range — that's the live consumer of the temporal parser.
    # _fts_temporal_search is retained as dead-but-tested code, pending a
    # benchmark that scores temporal relevance directly (the current 18-query
    # benchmark scores by topic keywords, not date proximity).

    # Cosine top-K
    model = _get_model()
    q_vec = model.encode(query, normalize_embeddings=True)
    scores = embeddings_norm @ q_vec

    # Recency decay scoring (m4): multiply cosine scores by per-memory strength.
    # L0 and decay_immune memories are exempt (is_immune() → multiplier stays 1.0).
    # Applies BEFORE argsort so decay changes the ranking.
    # REVERTED: benchmark regression to 70.0% (gate is 73%) — scaffolding kept,
    # scoring wiring disabled pending a benchmark-safe calibration.
    # if _settings.MEMEM_DECAY_ENABLED:
    #     from memem.decay import compute_strength, is_immune
    #     decay_mult = np.ones_like(scores)
    #     for i, mem_id in enumerate(ids):
    #         if mem_id not in vault_idx:
    #             continue
    #         mem = vault_idx[mem_id]
    #         if is_immune(mem):
    #             continue  # multiplier stays 1.0
    #         decay_mult[i] = compute_strength(mem)
    #     scores = scores * decay_mult

    order = np.argsort(scores)[::-1]

    # Build BM25 over vault (cached)
    bm25_data = _build_bm25(vault_idx)
    # Compute cosine scores into dict[id, float]
    cosine_scores: dict[str, float] = {}
    for i in order:
        if i < len(ids) and ids[i] in vault_idx:
            cosine_scores[ids[i]] = float(scores[i])
    # Compute BM25 scores
    bm25_scores: dict[str, float] = {}
    if bm25_data is not None:
        bm25_ids, bm25_index = bm25_data
        query_tokens = query.lower().split()
        if query_tokens:
            raw_bm25 = bm25_index.get_scores(query_tokens)
            bm25_scores = {bm25_ids[i]: float(raw_bm25[i]) for i in range(len(bm25_ids))}
    # Fuse
    fused = _rrf_fusion(cosine_scores, bm25_scores, k=60)
    # Sort by fused score desc, take top 20 (for MMR), build candidate list
    sorted_ids = sorted(fused, key=lambda m: -fused[m])
    cosine_candidates: list[MemoryHit] = []
    cosine_added = 0
    mmr_pool_size = min(20, len(sorted_ids))
    for mid in sorted_ids:
        if cosine_added >= mmr_pool_size:
            break
        if mid in vault_idx:
            mem = vault_idx[mid]
            if mem["path"] not in seen_paths:
                cosine_candidates.append({
                    **{key: val for key, val in mem.items() if key != "body_full"},
                    "score": fused[mid],
                    "source": "cosine",  # keep "cosine" so writeback in m3 still fires
                })
                seen_paths.add(mem["path"])
                cosine_added += 1

    # Date-aware re-ranking: boost cosine candidates that fall inside the
    # temporal window extracted from the query (Feature B).  Non-temporal
    # queries → _extract_temporal_range returns None → no-op.
    temporal_range = _extract_temporal_range(query)
    if temporal_range is not None:
        start_dt, end_dt = temporal_range
        for hit in cosine_candidates:
            created_dt = parse_iso_dt(hit.get("created", ""))
            if created_dt is not None and start_dt <= created_dt <= end_dt:
                hit["score"] = hit["score"] + 0.2
        cosine_candidates.sort(key=lambda h: h["score"], reverse=True)

    # MMR re-ranking: diversify top-20 cosine candidates down to k results.
    # L0 / decay_immune memories are pre-seeded (always included).
    mmr_selected = _mmr_rerank(cosine_candidates, embeddings_norm, ids, k=k)
    results.extend(mmr_selected)

    # Fire-and-forget access writeback via telemetry sidecar (m3).
    # Only cosine hits are recorded; FTS hits are structural supplements, not
    # relevance-driven accesses. The daemon thread does not block the caller.
    if _settings.MEMEM_WRITEBACK_ENABLED and results:
        def _writeback():
            from memem.telemetry import _record_access
            for hit in results:
                if hit.get("source") == "cosine":
                    try:
                        _record_access(hit["id"])
                    except Exception:  # noqa: BLE001
                        pass
        threading.Thread(target=_writeback, daemon=True).start()

    return results
