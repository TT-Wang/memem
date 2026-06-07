"""Candidate generation helpers for Active Memory Slice (extracted from active_slice_engine)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from memem.active_slice import (
    Candidate,
    CandidateBundle,
    current_query_candidate,
    normalize_artifact_candidate,
    normalize_memory_candidate,
    normalize_transcript_candidate,
)
from memem.artifact_context import artifact_candidates_from_environment
from memem.environment_context import (
    environment_candidates_from_environment,
    normalize_runtime_environment,
)
from memem.io_utils import atomic_write_text
from memem.models import LAYER_L0, LAYER_L3, _normalize_scope_id

log = structlog.get_logger("memem-active-slice")

_MAX_MEMORY_CANDIDATES = 20
_MAX_GRAPH_CANDIDATES = 20
_MAX_TRANSCRIPT_CANDIDATES = 5
_MAX_ARTIFACT_CANDIDATES = 8


def _dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    result: list[Candidate] = []
    for candidate in candidates:
        key = candidate.get("memory_id") or candidate.get("artifact_id") or candidate.get("candidate_id", "")
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _gather_l0_anchors(scope_id: str) -> list[Candidate]:
    """Always-include L0 (project identity) memories for the active scope.

    L0 memories are intrinsic to the project — they should be in every active
    slice for that scope, regardless of query relevance. Anchor score 0.95 so
    they survive reranking but don't dominate over highly-relevant L2 hits.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return []
    all_mems = _obsidian_memories()
    normalized = _normalize_scope_id(scope_id)
    l0_for_scope = [
        m for m in all_mems
        if m.get("layer") == LAYER_L0
        and _normalize_scope_id(str(m.get("project", "general") or "general")) == normalized
        and m.get("invalid_at") is None  # bi-temporal filter: skip invalidated anchors
    ]
    return [
        normalize_memory_candidate(m, source_reason="l0_anchor", score=0.95)
        for m in l0_for_scope
    ]


def _playbook_candidate(scope_id: str) -> Candidate | None:
    from memem import models

    normalized = _normalize_scope_id(scope_id)
    path = models.PLAYBOOK_DIR / f"{normalized}.md"
    if not path.exists():
        return None
    try:
        content = path.read_text(errors="ignore").strip()
    except OSError:
        return None
    if not content:
        return None
    return normalize_artifact_candidate("playbook", f"{normalized} playbook", content[:4000], path=str(path), score=0.72, project=normalized)


def _transcript_candidates(query: str) -> list[Candidate]:
    try:
        from memem.transcripts import transcript_search
        result = transcript_search(query, limit=_MAX_TRANSCRIPT_CANDIDATES)
    except Exception as exc:
        log.debug("transcript candidate generation failed", exc=exc)
        return []
    if not result or "No matching" in result:
        return []
    chunks = [chunk.strip() for chunk in result.split("\n\n") if chunk.strip()]
    return [
        normalize_transcript_candidate(chunk, title=f"Transcript excerpt {idx + 1}", score=0.45)
        for idx, chunk in enumerate(chunks[:_MAX_TRANSCRIPT_CANDIDATES])
    ]


def _detect_tie_zone(ranked: list[Candidate], k: int = 5, threshold: float = 0.10) -> bool:
    """Return True if the top-k candidates' scores are within `threshold` of each other.

    A 'tie zone' means the weighted-sum ranking is essentially indistinguishable —
    relative ordering within the zone is noise. Triggers tournament tie-break.
    """
    if len(ranked) < 2:
        return False
    top_k = ranked[:k]
    scores = [float(c.get("score", 0.0)) for c in top_k]
    return (max(scores) - min(scores)) <= threshold


def _tournament_break_ties(
    query: str,
    candidates: list[Candidate],
    cache_dir: Path,
) -> list[Candidate]:
    """Run pairwise Haiku judge on tied candidates; return re-ordered by win count.

    For N candidates, run min(N choose 2, 6) pairwise comparisons (cap at 6 to bound cost).
    Cache by (query-fingerprint, candidate-set-hash) at cache_dir / '.tournament-cache.json'
    with 24h TTL. Cache hit -> no Haiku call.
    """
    if not candidates:
        return candidates

    # Build fingerprint for cache lookup
    candidate_ids = sorted(
        c.get("memory_id") or c.get("artifact_id") or c.get("candidate_id", "")
        for c in candidates
    )
    raw_key = query + "".join(candidate_ids)
    fingerprint = hashlib.sha256(raw_key.encode()).hexdigest()[:16]

    # Check cache
    cache_file = cache_dir / ".tournament-cache.json"
    cache: dict[str, Any] = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except Exception:
            cache = {}

    now = datetime.now(UTC)
    entry = cache.get(fingerprint)
    if entry:
        ts_str = entry.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if now - ts < timedelta(hours=24):
                # Cache hit: reorder candidates by cached order
                order = entry.get("order", [])
                id_to_cand: dict[str, Candidate] = {}
                for c in candidates:
                    cid = c.get("memory_id") or c.get("artifact_id") or c.get("candidate_id", "")
                    id_to_cand[cid] = c
                reordered = [id_to_cand[oid] for oid in order if oid in id_to_cand]
                # Append any candidates not in cached order (shouldn't happen, but safety net)
                seen = set(order)
                for c in candidates:
                    cid = c.get("memory_id") or c.get("artifact_id") or c.get("candidate_id", "")
                    if cid not in seen:
                        reordered.append(c)
                return reordered
        except Exception:
            pass

    # Run pairwise tournament
    n = len(candidates)
    wins: dict[int, int] = dict.fromkeys(range(n), 0)

    # Build all pairs, cap at 6
    pairs: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append((i, j))
            if len(pairs) >= 6:
                break
        if len(pairs) >= 6:
            break

    for i, j in pairs:
        a = candidates[i]
        b = candidates[j]
        title_a = a.get("title", "")
        title_b = b.get("title", "")
        essence_a = (a.get("content") or a.get("summary") or "")[:300]
        essence_b = (b.get("content") or b.get("summary") or "")[:300]

        prompt = (
            f'For the query: "{query}"\n\n'
            "Which memory is more useful as context?\n\n"
            f"Memory A:\n{title_a}\n{essence_a}\n\n"
            f"Memory B:\n{title_b}\n{essence_b}\n\n"
            'Reply with exactly one character: "A" or "B".'
        )

        try:
            # Tournament cap: 30s per pairwise call (cheaper than mining's 180s).
            # Pairwise judge is a tiny prompt, so 30s is generous; we still want
            # to bound user-visible latency since tournament runs in the
            # synchronous slice-generation path (max 6 calls × 30s = 180s).
            tournament_timeout = int(os.environ.get("MEMEM_TOURNAMENT_TIMEOUT", "30"))
            result = subprocess.run(
                ["claude", "-p", "--model", "haiku", "--tools", "", "--system-prompt",
                 "You are a memory relevance judge. Reply with exactly one character: A or B."],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=tournament_timeout,
                env={**os.environ, "MEMEM_HOOK_DISABLE": "1"},
                start_new_session=True,
            )
            out = result.stdout.strip().upper()
            if out.startswith("A"):
                wins[i] += 1
            elif out.startswith("B"):
                wins[j] += 1
            # If unclear, no point awarded (tie)
        except Exception as exc:
            log.debug("tournament pairwise call failed", exc=exc)

    # Sort by win count descending; stable sort preserves original order on equal wins
    indexed = sorted(range(n), key=lambda i: -wins[i])
    reordered = [candidates[i] for i in indexed]

    # Persist to cache
    final_order = [
        c.get("memory_id") or c.get("artifact_id") or c.get("candidate_id", "")
        for c in reordered
    ]
    cache[fingerprint] = {"order": final_order, "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        atomic_write_text(cache_file, json.dumps(cache, indent=2))
    except Exception as exc:
        log.debug("tournament cache write failed", exc=exc)

    return reordered


def _graph_candidates(memory_candidates: list[Candidate]) -> list[Candidate]:
    graph: list[Candidate] = []
    try:
        from memem.graph_index import _NORMAL_RECALL_TYPES, _neighbors
        from memem.obsidian_store import _find_memory
    except Exception as exc:
        log.debug("graph candidate generation unavailable", exc=exc)
        return []

    for candidate in memory_candidates[:_MAX_MEMORY_CANDIDATES]:
        mid = candidate.get("memory_id", "")
        for edge in _neighbors(mid, relation_types=_NORMAL_RECALL_TYPES, limit=4, min_score=0.2):
            mem = _find_memory(edge.get("dst_id", ""))
            if not mem:
                continue
            graph.append(normalize_memory_candidate(
                mem,
                source_reason=f"graph:{edge.get('relation_type', 'related')}",
                score=float(edge.get("score", 0.4)),
                graph_distance=1,
            ))
            if len(graph) >= _MAX_GRAPH_CANDIDATES:
                return graph
    return graph


def generate_candidates(
    query: str,
    scope_id: str,
    environment: dict[str, Any] | None = None,
    limit: int = 20,
    *,
    use_llm: bool = True,
) -> CandidateBundle:
    """Generate bounded candidate pool for Active Memory Slice activation.

    v1.8.3: `use_llm` now also gates the Haiku tournament tie-break. Previous
    behaviour: the hook called with --no-llm to suppress LLM activation, but
    the tournament fired anyway under MEMEM_TOURNAMENT_ENABLED=true (default),
    burning Haiku latency on the synchronous hook hot-path. The whole point of
    --no-llm is "no Haiku from this code path" — honour it everywhere.
    """
    env = normalize_runtime_environment(environment)
    normalized_scope = _normalize_scope_id(scope_id)
    current = [current_query_candidate(query, normalized_scope)]

    l0_anchors = _gather_l0_anchors(normalized_scope)

    memory_candidates: list[Candidate] = []
    try:
        from memem.recall import _search_memories
        rerank_model: str | None = env.get("rerank_model") or None  # type: ignore[assignment]
        memories = _search_memories(
            query,
            scope_id=normalized_scope,
            limit=min(limit, _MAX_MEMORY_CANDIDATES),
            record_access=False,
            expand_links=False,
            rerank_model=rerank_model,
        )
        try:
            from memem import decay as _decay
        except Exception:
            _decay = None  # type: ignore[assignment]
        memory_candidates = []
        for idx, mem in enumerate(memories[:_MAX_MEMORY_CANDIDATES]):
            base_score = 0.75 - (idx * 0.02)
            if _decay is not None:
                try:
                    strength = _decay.compute_strength(mem)
                    # Clamp to [0, 1] so high-strength L2 hits cannot push the
                    # 0.95 L0 anchor out of the LLM activation top-K window.
                    # (compute_strength can return >1 for high-importance + high-access.)
                    base_score = min(1.0, base_score * strength)
                except Exception as _dexc:
                    log.debug("decay.compute_strength failed", memory_id=mem.get("id", "")[:8], exc=_dexc)
            memory_candidates.append(
                normalize_memory_candidate(mem, source_reason="recall", score=base_score)
            )
    except Exception as exc:
        log.debug("recall candidate generation failed", exc=exc)

    # Filter L3 (archival) memories — they must not appear in auto-recall slices.
    memory_candidates = [c for c in memory_candidates if c.get("layer") != LAYER_L3]

    # Dedup: L0 anchors come first so they win the score competition (0.95).
    # If an L0 memory also matched the relevance query it would appear twice;
    # dedup by memory_id removes the lower-scored relevance copy.
    seen_ids: set[str] = set()
    deduped_memory_candidates: list[Candidate] = []
    for cand in l0_anchors + memory_candidates:
        mem_id = cand.get("memory_id", "")
        if mem_id and mem_id in seen_ids:
            continue
        if mem_id:
            seen_ids.add(mem_id)
        deduped_memory_candidates.append(cand)
    memory_candidates = deduped_memory_candidates

    # A-MemGuard lesson exclusion: filter out memories flagged by lessons for this query.
    try:
        from memem.lessons import excluded_memory_ids_for_query
        excluded = excluded_memory_ids_for_query(query)
        if excluded:
            memory_candidates = [c for c in memory_candidates if c.get("memory_id") not in excluded]
    except Exception as exc:
        log.debug("lesson exclusion failed", exc=exc)

    # Project-scope precision re-ranking: boost in-scope memories, penalize
    # cross-project memories. General/empty-project memories are unchanged.
    for cand in memory_candidates:
        cand_project = _normalize_scope_id(str(cand.get("project", "") or ""))
        if not cand_project or cand_project == "general":
            continue  # cross-project default scope, no penalty
        if cand_project == normalized_scope:
            cand["score"] = min(1.0, float(cand.get("score", 0.0)) * 1.5)
        else:
            cand["score"] = float(cand.get("score", 0.0)) * 0.7

    graph_candidates = _graph_candidates(memory_candidates)
    playbook = _playbook_candidate(normalized_scope)
    transcript_setting = env.get("include_transcripts", os.environ.get("MEMEM_ACTIVE_SLICE_TRANSCRIPTS", "0"))
    transcript_flag = str(transcript_setting).lower()
    transcript_candidates = (
        _transcript_candidates(query)
        if transcript_flag in {"1", "true", "yes", "on"}
        else []
    )

    artifact_candidates = artifact_candidates_from_environment(env, normalized_scope)
    environment_candidates = environment_candidates_from_environment(env, normalized_scope)

    # Tournament tie-break: when top-K memory candidates are within threshold, use Haiku
    # to pairwise-rank them. Skipped when MEMEM_TOURNAMENT_ENABLED=false or no tie zone.
    _TOURNAMENT_K = 5
    merged_memory = _dedupe_candidates(
        (memory_candidates + graph_candidates)[:_MAX_MEMORY_CANDIDATES + _MAX_GRAPH_CANDIDATES]
    )
    tournament_enabled_raw = os.environ.get("MEMEM_TOURNAMENT_ENABLED", "true").lower()
    tournament_enabled = tournament_enabled_raw in {"1", "true", "yes", "on"}
    # v1.8.3: respect use_llm — callers that asked for no-LLM (e.g. the
    # synchronous UserPromptSubmit hook) must not pay tournament Haiku cost.
    if use_llm and tournament_enabled and _detect_tie_zone(merged_memory, k=_TOURNAMENT_K):
        from memem.models import MEMEM_DIR
        tied_top = merged_memory[:_TOURNAMENT_K]
        rest = merged_memory[_TOURNAMENT_K:]
        try:
            reranked_top = _tournament_break_ties(query, tied_top, cache_dir=MEMEM_DIR)
            merged_memory = reranked_top + rest
        except Exception as exc:
            log.debug("tournament tie-break failed (skipping)", exc=exc)

    return {
        "current_goal_candidates": current,
        "memory_candidates": merged_memory,
        "playbook_candidate": playbook,
        "transcript_candidates": transcript_candidates,
        "artifact_candidates": artifact_candidates[:_MAX_ARTIFACT_CANDIDATES],
        "environment_candidates": environment_candidates,
    }
