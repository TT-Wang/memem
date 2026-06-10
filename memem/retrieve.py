"""memem v2.6.0 retrieval — Three-way RRF (cosine+BM25+FTS) + rerank signals + scope param.

Benchmark-validated at ≥74% precision on 18-query × 6-category eval.
Replaces v2.5.0's two-channel (cosine+BM25) pipeline with a full One Engine.

No daemon. No hard scope filter. No kind classifier. No LLM judge.

v2.6.0 One Engine changes:
- FTS as third RRF channel: _search_fts top-20 results join cosine+BM25 in
  three-way RRF fusion for EVERY query (not just version/date literals).
  _rrf_fusion extended to accept an optional third fts_scores dict.
- scope_id parameter: retrieve() and load_vault_index() accept scope_id=''.
  Scope is a SOFT bonus only (never hard-filters).  FTS is called with the
  all-projects scope ("default") to get a global ranked list.
- Rerank signals (post min-max normalization, pre temporal boost):
    final_score = norm_rrf * (1 + w_use*usage + w_scope*scope + w_link*link + w_imp*imp)
  Scores may exceed 1.0 after multipliers — nothing downstream gates on magnitude.
  Pipeline order: 3-way RRF → min-max normalize [0,1] → signal multiplier bundle
  (usage/scope/link/imp) → temporal 1.2x multiplier → re-sort → MMR(λ=0.7) → k.
- Telemetry bulk load: TELEMETRY_FILE read once per retrieve() call (not per-memory).
- MEMEM_RERANK_MODEL env default lives in the recall layer (cross-encoder is a
  post-retrieve option there), not in this module.
- Calibrated weights (benchmark-gated ≥74%):
    w_use=0.15, w_scope=0.10, w_link=0.10, w_imp=0.10  (primary)
    Fallback if precision < 74%: w_use=0.05, w_scope=0.05, w_link=0.05, w_imp=0.05

v2.2.0 temporal awareness retained:
- _extract_temporal_range: parse "yesterday", "last week", "N days ago", etc.
- _fts_temporal_search: supplement results from the temporal date window
- retrieve() date-aware re-ranking: proportional 1.2x multiplier for on-window memories

v2.5.0 RRF scale fixes (B2, B3) retained:
- B2: After RRF fusion, fused scores are min-max normalized to [0,1] over the
  candidate pool BEFORE temporal re-ranking. Temporal boost is a bounded 1.2x
  multiplier (not additive +0.2).
- B3: _mmr_rerank now receives normalized [0,1] scores, so the diversity term
  (max cosine sim, also [0,1]) is on the same scale. lam=0.7 is retained.

Thread safety note: module-level caches (_vault_idx_cache, _emb_cache,
_model, _tel_cache) use a "set after compute" pattern that is safe under CPython's GIL.
Two threads may each compute the value and the second write wins, but both
values are equivalent — no torn state, no corruption. This is the intentional
design choice for simplicity over locking overhead.

_fts_literal_search decision: KEPT as prepend path (scores=1.0, placed before
RRF candidates in the result list). Benchmark comparison: folding it into RRF
only was tested and showed the pre-prepend path maintained benchmark parity.
Keeping the literal prepend path avoids changing the order semantics that callers
may depend on (literal matches guaranteed to be first).
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import numpy as np

import memem.recall_log as _recall_log
import memem.settings as _settings
from memem.models import MEMEM_DIR, OBSIDIAN_MEMORIES_DIR, TELEMETRY_FILE, parse_iso_dt, _normalize_scope_id

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
_STATUS_PAT = re.compile(r"^status:\s*(.*)$", re.M)
_INVALID_AT_PAT = re.compile(r"^invalid_at:\s*(.*)$", re.M)
_VERSION_PAT = re.compile(r"v\d+\.\d+(?:\.\d+)?", re.I)
_DATE_PAT = re.compile(r"\d{4}-\d{2}-\d{2}")

# Module-level caches (lazy-loaded, mtime-invalidated).
# CPython GIL makes "set after compute" safe — see module docstring.
_model: "Any | None" = None  # SentenceTransformer instance (lazy-loaded)
_vault_idx_cache: dict | None = None
_vault_idx_mtime: float = 0
_vault_idx_count: int = 0  # Phase 4.5 fix: also track file count so deletes invalidate
_emb_cache: tuple | None = None  # (normalized_embeddings, ids_list, mtime)
_bm25_cache: tuple | None = None  # (ids: list[str], bm25: BM25Okapi)
_bm25_cache_key: tuple = (0.0, 0)  # (max_mtime, file_count) — same key as vault cache


class MemoryHit(TypedDict, total=False):
    """A single retrieval result.

    score semantics (v2.6.0+):
    - FTS prepend hits (source="fts"): score=1.0 always (literal/version match).
    - RRF candidates (source="cosine"): base is min-max normalized to [0,1] over
      the candidate pool, then multiplied by the signal bundle
      (1 + w_use*usage + w_scope*scope + w_link*link + w_imp*imp), then optionally
      multiplied by the temporal 1.2x boost.  Scores may exceed 1.0 after multipliers;
      nothing downstream gates on magnitude — the ordering is what matters.
    """
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
    tags: list[str]
    related: list[str]
    status: str


def _get_model() -> "Any | None":
    """Lazily load the sentence-transformers model (singleton per process).

    Returns None if sentence-transformers is not installed (optional dep).
    retrieve() checks the return value and skips the cosine channel when None.
    """
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            _model = SentenceTransformer(_EMBEDDING_MODEL)
        except Exception:  # noqa: BLE001 — not just ImportError: OSError/RuntimeError
            # on a missing/corrupt model cache (first run, CI) must also degrade
            # to the BM25-only channel rather than crash retrieve().
            return None
    return _model


def load_vault_index(scope_id: str = "") -> dict[str, dict]:
    """Walk vault *.md files, extract id+title+project+created+body.

    The scope_id parameter is accepted for API compatibility; scope matching
    is a SOFT bonus applied in retrieve() — this function always returns the
    full vault index regardless of scope_id.

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
            # Limit frontmatter window to the closing '---' so body content
            # (which can contain 'status:', 'tags:', etc.) never bleeds into
            # frontmatter extraction.  Fall back to [:2000] if no closing
            # delimiter is found (malformed file) — existing behaviour.
            front_end = -1
            if text.startswith("---"):
                front_end = text.find("\n---", 4)
            if front_end > 0:
                front = text[: front_end + 4]  # include the closing '\n---'
            else:
                front = text[:2000]
            id_m = _ID_PAT.search(front)
            if not id_m:
                continue
            body = text
            if text.startswith("---") and front_end > 0:
                body = text[front_end + 4:].lstrip("\n")
            elif text.startswith("---"):
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
            # status extraction (default 'active' when key absent)
            status_m = _STATUS_PAT.search(front)
            status_val = (status_m.group(1) if status_m else "active").strip().strip("'\"")
            if not status_val:
                status_val = "active"
            # invalid_at extraction — key only present when set
            invalid_at_m = _INVALID_AT_PAT.search(front)
            invalid_at_val = (invalid_at_m.group(1) if invalid_at_m else "").strip().strip("'\"")
            # EXCLUSION: skip deprecated memories and memories with a non-empty invalid_at
            if status_val == "deprecated" or invalid_at_val:
                continue
            # tags extraction — handle both block list and inline-empty `tags: []`.
            # `(?:\n|$)` tolerates a missing trailing newline on the last item
            # (non-canonical/imported files); empty items (`- `) are filtered.
            tags_block_m = re.search(r"^tags:\n((?:- .*(?:\n|$))+)", front, re.M)
            if tags_block_m:
                tags_val = re.findall(r"^- (.+)$", tags_block_m.group(1), re.M)
                tags_val = [s for t in tags_val if (s := t.strip().strip("'\""))]
            else:
                tags_val = []
            # related extraction — block list of 8-char hex prefixes (possibly quoted)
            related_block_m = re.search(r"^related:\n((?:- .*(?:\n|$))+)", front, re.M)
            if related_block_m:
                related_val = re.findall(r"^- (.+)$", related_block_m.group(1), re.M)
                related_val = [s for r in related_val if (s := r.strip().strip("'\""))]
            else:
                related_val = []
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
                "status": status_val,
                "tags": tags_val,
                "related": related_val,
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
    fts_scores: dict[str, float] | None = None,
) -> dict[str, float]:
    """Standard RRF: score = sum of 1/(k + rank) across active channels.

    Supports 2-channel (cosine+BM25) or 3-channel (cosine+BM25+FTS) fusion.
    fts_scores is optional; when provided, FTS rankings contribute a third
    1/(k+rank) term for every ID that appears in the FTS result list.
    IDs absent from a channel get a penalty rank = max(channel_size) + 1.
    """
    cosine_ranks = {mid: r for r, (mid, _) in enumerate(
        sorted(cosine_scores.items(), key=lambda x: -x[1]), start=1)}
    bm25_ranks = {mid: r for r, (mid, _) in enumerate(
        sorted(bm25_scores.items(), key=lambda x: -x[1]), start=1)}
    fts_ranks: dict[str, int] = {}
    if fts_scores:
        fts_ranks = {mid: r for r, (mid, _) in enumerate(
            sorted(fts_scores.items(), key=lambda x: -x[1]), start=1)}
    all_ids = set(cosine_ranks) | set(bm25_ranks) | set(fts_ranks)
    missing_cosine = len(cosine_ranks) + 1
    missing_bm25 = len(bm25_ranks) + 1
    missing_fts = len(fts_ranks) + 1
    out: dict[str, float] = {}
    for mid in all_ids:
        c_rank = cosine_ranks.get(mid, missing_cosine)
        b_rank = bm25_ranks.get(mid, missing_bm25)
        score = 1.0 / (k + c_rank) + 1.0 / (k + b_rank)
        if fts_ranks:
            f_rank = fts_ranks.get(mid, missing_fts)
            score += 1.0 / (k + f_rank)
        out[mid] = score
    return out


_tel_cache: tuple | None = None  # (data_dict, mtime) — same pattern as _emb_cache


def _load_telemetry_bulk() -> dict:
    """Read TELEMETRY_FILE once per file-change and return its content dict.

    Keys in the telemetry file are 8-char memory id prefixes.
    Returns empty dict on missing or corrupt file — always tolerant.

    mtime-cached (the file is written via atomic os.replace, so st_mtime is a
    reliable invalidation key — same pattern as _emb_cache/_vault_idx_cache).
    telemetry.json grows one key per ever-accessed memory and never shrinks;
    without the cache every retrieve() call would pay an O(file-size) parse
    (~0.7ms at 1k entries, ~44ms at 50k).
    """
    global _tel_cache
    try:
        if not TELEMETRY_FILE.exists():
            return {}
        mtime = TELEMETRY_FILE.stat().st_mtime
        if _tel_cache is not None and _tel_cache[1] >= mtime:
            return _tel_cache[0]
        data = json.loads(TELEMETRY_FILE.read_text())
        _tel_cache = (data, mtime)
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _rerank_signals(
    candidates: list[MemoryHit],
    tel_data: dict,
    scope_id: str,
    vault_idx: dict,
) -> dict[str, dict[str, float]]:
    """Compute per-memory rerank signals for a list of candidates.

    Returns a dict mapping memory_id → {usage, scope, link, imp} signals
    each in [0, 1].

    Signals:
    - usage: 0.5 * recency_decay(hours_since_last_access, half_life=7d)
             + 0.5 * min(1, log1p(access_count) / log1p(20))
    - scope: 1.0 if memory.project == normalized_scope_id else 0.0
             (non-empty scope_id only; '' → no bonus)
    - link: 1.0 if this memory is in the related[] of any top-5 seed candidate
            (forward: seed.related contains this id[:8])
            OR if this memory's related[] contains a top-5 seed's id[:8]
            (reverse: 1-hop, in-memory, zero extra I/O)
    - imp: importance / 5.0 (importance is 1-5, default 3)
    """
    import math
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    norm_scope = _normalize_scope_id(scope_id) if scope_id else ""

    # Identify top-5 seeds for link signal (by current score order)
    top5 = candidates[:5]
    # Forward: union of related[] from each top-5 seed (8-char prefixes).
    # A seed's own prefix is excluded from ITS contribution so bad data
    # (a memory listing itself in related[]) can't earn a self-bonus,
    # while legitimate seed→seed links are preserved.
    seed_related_prefixes: set[str] = set()
    for seed in top5:
        seed_own = (seed.get("id") or "")[:8]
        for rel in seed.get("related", []):
            if rel != seed_own:
                seed_related_prefixes.add(rel)
    # Reverse: set of top-5 seed id[:8] prefixes (for reverse lookup)
    seed_id_prefixes: set[str] = {s["id"][:8] for s in top5 if s.get("id")}

    signals: dict[str, dict[str, float]] = {}
    for hit in candidates:
        mid = hit.get("id", "")
        # --- usage signal ---
        tel_entry = tel_data.get(mid[:8], {})
        last_accessed_str = tel_entry.get("last_accessed", "") or hit.get("last_accessed_at", "")
        access_count = tel_entry.get("access_count", 0) or hit.get("access_count", 0)

        recency = 0.0
        if last_accessed_str:
            from memem.models import parse_iso_dt as _parse
            last_dt = _parse(last_accessed_str)
            if last_dt is not None:
                hours_since = (now - last_dt).total_seconds() / 3600.0
                # Recency decay: exponential half-life of 7 days (168 hours)
                # decay(t) = 2^(-t / half_life_hours) → at t=0: 1.0; at t=7d: 0.5
                recency = 2.0 ** (-hours_since / 168.0)

        freq = min(1.0, math.log1p(access_count) / math.log1p(20))
        usage = 0.5 * recency + 0.5 * freq

        # --- scope signal ---
        scope_sig = 0.0
        if norm_scope:
            mem_project = _normalize_scope_id(hit.get("project", "") or "")
            scope_sig = 1.0 if mem_project == norm_scope else 0.0

        # --- link signal ---
        link_sig = 0.0
        if mid:
            # Forward: mid[:8] in any seed's related[]
            if mid[:8] in seed_related_prefixes:
                link_sig = 1.0
            else:
                # Reverse: any of this memory's related[] prefixes match a seed id prefix.
                # Skip only true self-links: a prefix that matches the candidate's own id.
                mem_related = hit.get("related", [])
                for rel_prefix in mem_related:
                    if rel_prefix in seed_id_prefixes and rel_prefix != mid[:8]:
                        link_sig = 1.0
                        break

        # --- importance signal ---
        importance = hit.get("importance", 3)
        imp_sig = float(importance) / 5.0

        signals[mid] = {
            "usage": usage,
            "scope": scope_sig,
            "link": link_sig,
            "imp": imp_sig,
        }

    return signals


# Calibrated rerank weights (benchmark-gated ≥74%):
# Primary: w_use=0.15, w_scope=0.10, w_link=0.10, w_imp=0.10
# Fallback (if primary drops precision below 74%):
#   w_use=0.05, w_scope=0.05, w_link=0.05, w_imp=0.05
# Final calibrated weights used in production (measured at benchmark run):
_W_USE = 0.15
_W_SCOPE = 0.10
_W_LINK = 0.10
_W_IMP = 0.10


def retrieve(
    query: str,
    k: int = 8,
    log_call_type: str | None = "hook_auto",
    scope_id: str = "",
    writeback: bool = True,
) -> list[MemoryHit]:
    """Main retrieval: three-way RRF (cosine+BM25+FTS) + rerank signals + scope bonus.

    Args:
        query: Natural language query string.
        k: Maximum number of results to return.
        log_call_type: Label for the recall_log entry (e.g. "hook_auto",
           "cli_slice"). When None, retrieve() skips its own log_recall call
           entirely — callers that log their own telemetry (e.g. server.py
           active_memory_slice) should pass None to prevent double-logging.
        scope_id: Optional project scope. When non-empty, memories in the same
           project receive a soft bonus (never a hard filter). Default '' = no bonus.
        writeback: When False, skip the fire-and-forget access-count writeback
           thread. Callers that record access themselves (recall._search_memories
           with record_access=True) pass False to prevent double-counting the
           same retrieval in the telemetry sidecar.

    Returns:
        List of MemoryHit dicts ordered FTS-literal-first, then by reranked score
        descending after MMR. Each hit has keys: id, path, title, project, created,
        body, score, source.

    Pipeline (v2.6.0):
        1. FTS literal prepend (version/date queries only, score=1.0)
        2. Cosine channel (sentence-transformers embedding similarity)
        3. BM25 channel (keyword relevance over title+body)
        4. FTS channel (SQLite FTS5, all-projects scope, top-20)
        5. Three-way RRF fusion → top-20 candidate pool
        6. Min-max normalize [0,1] over candidate pool (B2 fix)
        7. Rerank signal bundle: score *= (1 + w_use*usage + w_scope*scope
                                            + w_link*link + w_imp*imp)
           (Scores may exceed 1.0 after multipliers — ordering is what matters)
        8. Temporal 1.2x multiplier for in-window memories (non-temporal → no-op)
        9. Re-sort by score desc
       10. MMR(λ=0.7) diversification → k results

    Score semantics:
        - FTS literal hits (source="fts"): score=1.0 always.
        - RRF candidates (source="cosine"): min-max normalized base, multiplied by
          signal bundle, optionally boosted by temporal 1.2x. May exceed 1.0.
    """
    t0 = time.monotonic()
    vault_idx = load_vault_index()
    if not vault_idx:
        return []
    emb_data = load_embeddings()
    # emb_data is None when embeddings.npy / embedding_ids.json are absent.
    # When sentence-transformers is also absent, we skip the cosine channel
    # entirely (degrade to FTS/BM25-only, mirroring the None-embeddings path).
    embeddings_norm: np.ndarray | None = None
    ids: list[str] = []
    if emb_data is not None:
        embeddings_norm, ids = emb_data

    # Bulk telemetry load — read TELEMETRY_FILE exactly once per retrieve() call.
    # Keys are 8-char memory id prefixes. Tolerates missing/corrupt file → {}.
    tel_data = _load_telemetry_bulk()

    results: list[MemoryHit] = []
    seen_paths: set[str] = set()

    # FTS literal prepend path: fires only when query has version/date literal.
    # Kept as prepend (not folded into RRF) to guarantee literal matches appear first.
    # Benchmark test: folding showed parity; keeping separate preserves ordering semantics.
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

    # Cosine channel — skipped gracefully when sentence-transformers is absent
    # or when embeddings haven't been built yet.
    model = _get_model()
    cosine_scores: dict[str, float] = {}
    if model is not None and embeddings_norm is not None:
        q_vec = model.encode(query, normalize_embeddings=True)
        scores = embeddings_norm @ q_vec

        order = np.argsort(scores)[::-1]
        for i in order:
            if i < len(ids) and ids[i] in vault_idx:
                cosine_scores[ids[i]] = float(scores[i])

    # BM25 channel (cached)
    bm25_data = _build_bm25(vault_idx)
    bm25_scores: dict[str, float] = {}
    if bm25_data is not None:
        bm25_ids, bm25_index = bm25_data
        query_tokens = query.lower().split()
        if query_tokens:
            raw_bm25 = bm25_index.get_scores(query_tokens)
            bm25_scores = {bm25_ids[i]: float(raw_bm25[i]) for i in range(len(bm25_ids))}

    # FTS channel — third RRF channel for EVERY query (not just version/date literals).
    # Call _search_fts with all-projects scope ("default" → normalized to "general" → no filter).
    # This gives us a rank-ordered list from SQLite FTS5; we assign positional scores
    # (N, N-1, ..., 1) for RRF rank computation.
    fts_scores: dict[str, float] = {}
    try:
        from memem.search_index import _search_fts  # noqa: PLC0415 — lazy import
        fts_ids = _search_fts(query, scope_id="default", limit=20)
        n_fts = len(fts_ids)
        for rank_idx, fts_mid in enumerate(fts_ids):
            if fts_mid in vault_idx:  # only include IDs present in active vault
                fts_scores[fts_mid] = float(n_fts - rank_idx)
    except Exception:  # noqa: BLE001 — FTS channel is optional; degrade gracefully
        pass

    # Three-way RRF fusion: cosine + BM25 + FTS
    # Works even if one or more channels are empty — degrades gracefully.
    fused = _rrf_fusion(cosine_scores, bm25_scores, k=60, fts_scores=fts_scores or None)
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

    # Min-max normalize fused scores to [0, 1] over the candidate pool (B2/B3 fix).
    # This ensures signal bundle, temporal boost and MMR diversity term operate on the
    # same scale. Raw RRF scores are in ~[1/(60+N), 3/61≈0.049] with 3 channels.
    # Normalization must happen BEFORE temporal boost and signal bundle.
    # Degenerate case (all scores equal): map to 1.0 (all equivalent relevance).
    if cosine_candidates:
        raw_scores = [h["score"] for h in cosine_candidates]
        score_min = min(raw_scores)
        score_max = max(raw_scores)
        if score_max > score_min:
            for hit in cosine_candidates:
                hit["score"] = (hit["score"] - score_min) / (score_max - score_min)
        else:
            # All scores identical — map to 1.0 (degenerate pool, all equally relevant)
            for hit in cosine_candidates:
                hit["score"] = 1.0

    # Rerank signal bundle (v2.6.0): applied AFTER min-max normalization, BEFORE temporal boost.
    # final_score = norm_rrf * (1 + w_use*usage + w_scope*scope + w_link*link + w_imp*imp)
    # Scores may exceed 1.0; nothing downstream gates on magnitude — ordering is what matters.
    if cosine_candidates:
        signals = _rerank_signals(cosine_candidates, tel_data, scope_id, vault_idx)
        for hit in cosine_candidates:
            mid = hit.get("id", "")
            sig = signals.get(mid, {"usage": 0.0, "scope": 0.0, "link": 0.0, "imp": 0.0})
            multiplier = (1.0
                          + _W_USE * sig["usage"]
                          + _W_SCOPE * sig["scope"]
                          + _W_LINK * sig["link"]
                          + _W_IMP * sig["imp"])
            hit["score"] = hit["score"] * multiplier

    # Date-aware re-ranking: boost cosine candidates that fall inside the
    # temporal window extracted from the query (Feature B).  Non-temporal
    # queries → _extract_temporal_range returns None → no-op.
    # Boost is a bounded 1.2x multiplier (not additive) so that in-window memories
    # are promoted proportionally to their relevance rather than unconditionally
    # jumping above all out-of-window matches. A weakly-relevant in-window memory
    # gets score × 1.2 while a highly-relevant out-of-window one retains its
    # normalized score — the latter can still win if it is sufficiently stronger.
    # Scores after boost remain in [0, ~1.2+]; post-boost sort preserves relative order.
    temporal_range = _extract_temporal_range(query)
    if temporal_range is not None:
        start_dt, end_dt = temporal_range
        for hit in cosine_candidates:
            created_dt = parse_iso_dt(hit.get("created", ""))
            if created_dt is not None and start_dt <= created_dt <= end_dt:
                hit["score"] = hit["score"] * 1.2
        cosine_candidates.sort(key=lambda h: h["score"], reverse=True)

    # MMR re-ranking: diversify top-20 cosine candidates down to k results.
    # L0 / decay_immune memories are pre-seeded (always included).
    # Pass empty array when embeddings unavailable — _mmr_rerank handles it.
    _emb_for_mmr = embeddings_norm if embeddings_norm is not None else np.zeros((0, 1), dtype=np.float32)
    mmr_selected = _mmr_rerank(cosine_candidates, _emb_for_mmr, ids, k=k)
    results.extend(mmr_selected)

    # Fire-and-forget access writeback via telemetry sidecar (m3).
    # Only cosine hits are recorded; FTS hits are structural supplements, not
    # relevance-driven accesses. The daemon thread does not block the caller.
    # Suppressed via writeback=False when the caller records access itself
    # (recall._search_memories) — prevents double-counting one retrieval.
    if _settings.MEMEM_WRITEBACK_ENABLED and writeback and results:
        def _writeback():
            from memem.telemetry import _record_access  # noqa: PLC0415
            for hit in results:
                if hit.get("source") == "cosine":
                    try:
                        _record_access(hit["id"])
                    except Exception:  # noqa: BLE001
                        pass
        threading.Thread(target=_writeback, daemon=True).start()

    # v2.4.0 telemetry — log every retrieve() call (called from hook in auto/hybrid mode).
    # When log_call_type is None, skip logging entirely (caller handles it themselves).
    if log_call_type is not None:
        try:
            _recall_log.log_recall(
                call_type=log_call_type,
                query=query,
                returned_ids=[h.get("id", "") for h in results],
                latency_ms=int((time.monotonic() - t0) * 1000),
                source="hook",
            )
        except Exception:  # noqa: BLE001
            pass
    return results
