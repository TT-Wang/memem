"""Active Memory Slice Engine orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import memem.settings as _memem_settings
from memem.activation import judge_activation_heuristically, judge_activation_with_llm
from memem.active_slice import (
    ActivationResult,
    ActiveMemorySlice,
    Candidate,
    CandidateBundle,
    DeltaWritebackResult,
    WritebackSummary,
    build_active_memory_slice,
    current_query_candidate,
    flatten_candidate_bundle,
    normalize_artifact_candidate,
    normalize_memory_candidate,
    normalize_transcript_candidate,
    render_slice_as_compact_context,
    render_slice_as_prompt_context,
)
from memem.active_slice_metrics import summarize_slice_metrics
from memem.artifact_context import artifact_candidates_from_environment
from memem.boundaries import apply_post_boundaries, apply_pre_boundaries
from memem.delta import propose_deltas_from_slice
from memem.delta_commit import commit_deltas
from memem.delta_policy import evaluate_delta_proposals
from memem.environment_context import (
    environment_candidates_from_environment,
    normalize_runtime_environment,
)
from memem.io_utils import atomic_write_text
from memem.models import LAYER_L0, LAYER_L3, _normalize_scope_id, parse_iso_dt
from memem.slice_history import (
    annotate_slice_continuity,
    get_empty_streak,
    increment_empty_streak,
    increment_turn_count,
    load_slice_history,
    persist_slice_history,
    reset_empty_streak,
)

log = logging.getLogger("memem-active-slice")

_MAX_MEMORY_CANDIDATES = 20
_MAX_GRAPH_CANDIDATES = 20
_MAX_TRANSCRIPT_CANDIDATES = 5
_MAX_ARTIFACT_CANDIDATES = 8
PromptContextMode = Literal["slice", "assembly"]
WritebackExecutionMode = Literal["policy_only", "preview", "commit"]

_OOV_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "and", "or", "but", "not",
    "to", "of", "in", "on", "at", "for", "with", "by", "as", "from",
})


def _detect_out_of_vault(query: str, candidate_bundle: CandidateBundle, threshold: float) -> bool:
    """Return True if query has no vault match.

    Heuristic: max memory candidate score < threshold AND no L0 anchor's
    title/tags contain any query token. Returns False (in-vault) when
    threshold <= 0.0 to act as a clean opt-out guard.
    """
    import re
    if threshold <= 0.0:
        return False
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    memory_candidates = [c for c in all_candidates if c.get("candidate_type") == "memory"]
    max_memory_score = max((float(c.get("score", 0.0)) for c in memory_candidates), default=0.0)
    if max_memory_score >= threshold:
        return False
    # Check L0 anchor overlap
    query_tokens = set(re.findall(r'\w{3,}', query.lower())) - _OOV_STOPWORDS
    if not query_tokens:
        return False  # too-short query — don't claim out-of-vault
    l0_anchors = [c for c in all_candidates if c.get("layer") == LAYER_L0]
    for anchor in l0_anchors:
        title = str(anchor.get("title", "")).lower()
        if any(tok in title for tok in query_tokens):
            return False
    return True


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
        log.debug("transcript candidate generation failed: %s", exc)
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
            log.debug("tournament pairwise call failed: %s", exc)

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
        log.debug("tournament cache write failed: %s", exc)

    return reordered


def _graph_candidates(memory_candidates: list[Candidate]) -> list[Candidate]:
    graph: list[Candidate] = []
    try:
        from memem.graph_index import _NORMAL_RECALL_TYPES, _neighbors
        from memem.obsidian_store import _find_memory
    except Exception as exc:
        log.debug("graph candidate generation unavailable: %s", exc)
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
                    log.debug("decay.compute_strength failed for %s: %s", mem.get("id", "")[:8], _dexc)
            memory_candidates.append(
                normalize_memory_candidate(mem, source_reason="recall", score=base_score)
            )
    except Exception as exc:
        log.debug("recall candidate generation failed: %s", exc)

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
        log.debug("lesson exclusion failed: %s", exc)

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
            log.debug("tournament tie-break failed (skipping): %s", exc)

    return {
        "current_goal_candidates": current,
        "memory_candidates": merged_memory,
        "playbook_candidate": playbook,
        "transcript_candidates": transcript_candidates,
        "artifact_candidates": artifact_candidates[:_MAX_ARTIFACT_CANDIDATES],
        "environment_candidates": environment_candidates,
    }


def build_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = False,
) -> ActiveMemorySlice:
    """Build and return a structured ActiveMemorySlice dict without rendering.

    This is the preferred entry point for callers that need the slice structure
    (e.g. context_assemble merging multiple slices). LLM activation is disabled
    by default to keep assembly fast; callers can opt in by passing use_llm=True.

    History persistence is intentionally disabled: assembly callers will call
    this for multiple scopes and should not create spurious history records.
    """
    return _generate_active_memory_slice_internal(
        query,
        scope_id=scope_id,
        environment=environment,
        use_llm=use_llm,
        writeback_mode="policy_only",
        auto_commit_safe=False,
        dry_run=True,
        persist_history=False,
    )


def _make_gating_stub(
    query: str,
    scope_id: str,
    session_id: str,
    gating_reason: str,
) -> ActiveMemorySlice:
    """Return a minimal ActiveMemorySlice that signals the caller to skip injection."""
    return cast(ActiveMemorySlice, {
        "slice_id": "",
        "session_id": session_id,
        "scope_id": scope_id,
        "query": query,
        "input_goal": query,
        "generated_at": "",
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "delta_results": [],
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "should_emit_context": False,
        "activation_mode": "heuristic",
        "confidence": 0.0,
        "warnings": [],
        "items": [],
        "slice_kind": "active",
        "gating_reason": gating_reason,
    })


def generate_active_memory_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> ActiveMemorySlice:
    """Main Active Memory Slice Engine entrypoint.

    v1.9 gating: unless MEMEM_INJECTION_MODE=='auto', trivial queries (short
    acknowledgements, slash-commands) are short-circuited before the full
    pipeline.  Non-trivial queries are subject to cadence gating with
    exponential backoff when the pipeline repeatedly returns empty results.
    """
    # -----------------------------------------------------------------
    # Read ALL gating constants from memem.settings (NOT from environment).
    # The normalize_runtime_environment() whitelist silently drops unknown
    # keys, making env-dict gating unreliable.
    # -----------------------------------------------------------------
    injection_mode = _memem_settings.MEMEM_INJECTION_MODE

    # 'auto' → legacy behaviour, skip gating entirely.
    # Also skip gating when no session_id is provided — per-session counters
    # are meaningless without a stable session identity.
    #
    # CARVE-OUT: when session_id is empty, gating is silently skipped and the
    # full pipeline runs. Callers (CLI smoke tests, ad-hoc scripts) that omit
    # session_id will NOT see hybrid/tool mode behaviour. Hook callers always
    # pass session_id, so this affects only opt-out / debug code paths.
    env = environment or {}
    session_id = str(env.get("session_id", "") or "")
    if injection_mode != "auto" and session_id:

        # --- Gate 1: trivial query (regex) ---
        stripped = query.strip()
        if _memem_settings.MEMEM_TRIVIAL_REGEX_EN.fullmatch(stripped):
            return _make_gating_stub(query, scope_id, session_id, "trivial_query")
        if _memem_settings.MEMEM_TRIVIAL_REGEX_ZH.fullmatch(stripped):
            return _make_gating_stub(query, scope_id, session_id, "trivial_query")
        if stripped.startswith("/"):
            return _make_gating_stub(query, scope_id, session_id, "slash_command")

        # --- Gate 2: turn cadence with empty-streak backoff ---
        turn = increment_turn_count(session_id)
        base_cadence = _memem_settings.MEMEM_INJECT_CADENCE
        streak = get_empty_streak(session_id)
        if streak > 0:
            max_multiplier = _memem_settings.MEMEM_EMPTY_STREAK_MAX
            effective_cadence = min(base_cadence * (2 ** streak), base_cadence * max_multiplier)
        else:
            effective_cadence = base_cadence
        # Turn 1 always runs, then every Nth turn thereafter.
        # (turn-1) % cadence == 0  →  run; otherwise skip.
        if (turn - 1) % effective_cadence != 0:
            return _make_gating_stub(query, scope_id, session_id, "cadence_skip")

    # -----------------------------------------------------------------
    # Gate 3: topic-shift detection via cached query embedding.
    # Only active when injection_mode != 'auto' and a session_id exists.
    # -----------------------------------------------------------------
    _current_emb: list[float] | None = None
    _topic_shift_active = injection_mode != "auto" and bool(session_id)
    if _topic_shift_active:
        try:
            from memem.embedding_index import _get_model  # singleton, do not re-instantiate
            model = _get_model()
            if model is not None:
                _current_emb = model.encode(
                    query, convert_to_numpy=True, show_progress_bar=False,
                ).tolist()
        except Exception:  # noqa: BLE001 — silently skip on any failure
            _current_emb = None

        if _current_emb is not None:
            try:
                from memem.slice_daemon import (
                    get_cached_query_embedding,
                    get_cached_slice,
                )
                _cached_emb = get_cached_query_embedding(session_id)
                _cached_slice = get_cached_slice(session_id)
                if _cached_emb is not None and _cached_slice is not None:
                    try:
                        import numpy as _np
                        _a = _np.asarray(_current_emb, dtype="float32")
                        _b = _np.asarray(_cached_emb, dtype="float32")
                        _norm_a = _np.linalg.norm(_a)
                        _norm_b = _np.linalg.norm(_b)
                        _sim = (
                            float(_np.dot(_a, _b) / (_norm_a * _norm_b))
                            if _norm_a > 0 and _norm_b > 0
                            else 0.0
                        )
                    except ImportError:
                        # Pure-Python fallback for cosine similarity
                        _dot = sum(x * y for x, y in zip(_current_emb, _cached_emb, strict=False))
                        _mag_a = sum(x * x for x in _current_emb) ** 0.5
                        _mag_b = sum(x * x for x in _cached_emb) ** 0.5
                        _sim = _dot / (_mag_a * _mag_b) if _mag_a > 0 and _mag_b > 0 else 0.0

                    if _sim >= _memem_settings.MEMEM_TOPIC_SHIFT_THRESHOLD:
                        # Topic unchanged — reuse cached slice. Overwrite the identity
                        # fields so downstream telemetry attributes the reuse to the
                        # CURRENT turn, not the cached previous turn.
                        from datetime import datetime
                        _reuse = cast(ActiveMemorySlice, dict(_cached_slice))
                        _reuse["gating_reason"] = "topic_shift_reuse"
                        _reuse["query"] = query
                        _reuse["session_id"] = session_id
                        _reuse["scope_id"] = scope_id
                        _reuse["generated_at"] = datetime.now(UTC).isoformat()
                        if not _cached_slice.get("should_emit_context", True):
                            pass  # C5: stale low-confidence cache must not be reused — fall through to full pipeline
                        else:
                            return _reuse
            except Exception:  # noqa: BLE001 — silently fall through on any failure
                pass

    # -----------------------------------------------------------------
    # Full pipeline — gating passed (or auto mode).
    # Wrap in try/except so a pipeline failure still observes the empty-streak
    # counter (treat failure as empty result so backoff kicks in).
    # -----------------------------------------------------------------
    try:
        result = _generate_active_memory_slice_internal(
            query,
            scope_id=scope_id,
            environment=environment,
            use_llm=use_llm,
            writeback_mode="policy_only",
            auto_commit_safe=False,
            dry_run=True,
            persist_history=True,
        )
    except Exception:
        if injection_mode != "auto" and session_id:
            increment_empty_streak(session_id)
        raise

    # After full pipeline: update the embedding+slice cache if we computed an embedding.
    if _topic_shift_active and _current_emb is not None:
        try:
            from memem.slice_daemon import set_cached_embedding_and_slice
            set_cached_embedding_and_slice(session_id, _current_emb, result)
        except Exception:  # noqa: BLE001 — cache update failure must never break recall
            pass

    # Observe result for empty-streak tracking (only when gating is active with session).
    if injection_mode != "auto" and session_id:
        sid = session_id
        memories = result.get("items") or []
        has_content = bool(
            memories
            or result.get("goals")
            or result.get("constraints")
            or result.get("active_background")
            or result.get("artifacts")
        )
        if result.get("should_emit_context") and has_content:
            reset_empty_streak(sid)
        else:
            increment_empty_streak(sid)

    return result


def _load_previous_slice(environment: dict[str, Any], scope_id: str) -> ActiveMemorySlice | None:
    continuity_mode = str(environment.get("continuity_mode", "") or "")
    if not continuity_mode or continuity_mode == "off":
        return None

    limit = int(environment.get("continuity_slice_limit", 10) or 10)
    session_id = str(environment.get("session_id", "") or "")
    if not session_id:
        return None

    records = load_slice_history(
        scope_id=scope_id,
        session_id=session_id,
        limit=max(limit, 1),
    )
    return records[-1] if records else None


def _continuity_context_slice(previous_slice: ActiveMemorySlice) -> ActiveMemorySlice:
    context_slice = cast(ActiveMemorySlice, dict(previous_slice))
    context_slice["candidate_deltas"] = []
    context_slice["delta_results"] = []
    context_slice["warnings"] = []
    context_slice["writeback_summary"] = {
        "status": "not_run",
        "dry_run": True,
        "proposed_count": 0,
        "auto_committed_count": 0,
        "manual_review_count": 0,
        "blocked_count": 0,
        "rejected_count": 0,
    }
    return context_slice


def _continuity_environment(
    environment: dict[str, Any],
    previous_slice: ActiveMemorySlice | None,
) -> dict[str, Any]:
    enriched = dict(environment)
    if not previous_slice:
        return enriched

    continuity_context = render_slice_as_compact_context(_continuity_context_slice(previous_slice), max_chars=1200)
    if continuity_context:
        enriched["continuity_context"] = continuity_context
    enriched["previous_slice_id"] = str(previous_slice.get("slice_id", "") or "")
    enriched["previous_constraint_ids"] = [
        str(item.get("memory_id", "") or "")
        for item in previous_slice.get("constraints", [])
        if str(item.get("memory_id", "") or "")
    ]
    enriched["previous_artifact_ids"] = [
        str(item.get("artifact_id", "") or "")
        for item in previous_slice.get("artifacts", [])
        if str(item.get("artifact_id", "") or "")
    ]
    enriched["previous_artifact_paths"] = [
        str(item.get("path", "") or "")
        for item in previous_slice.get("artifacts", [])
        if str(item.get("path", "") or "")
    ]
    enriched["previous_open_tension_descriptions"] = [
        str(item.get("description", "") or "")
        for item in previous_slice.get("open_tensions", [])
        if str(item.get("description", "") or "")
    ]
    return enriched


def _policy_result_status(decision: dict[str, Any]) -> str:
    if decision.get("decision") == "reject" or decision.get("commit_policy") == "blocked":
        return "rejected"
    return "not_run"


def _policy_results(
    candidate_deltas: list[dict[str, Any]],
    policy_decisions: list[dict[str, Any]],
) -> list[DeltaWritebackResult]:
    results: list[DeltaWritebackResult] = []
    for delta, decision in zip(candidate_deltas, policy_decisions, strict=False):
        target_memory_ids = [
            str(memory_id)[:8]
            for memory_id in decision.get("target_memory_ids", delta.get("target_memory_ids", []))
            if str(memory_id)
        ]
        results.append({
            "delta_id": str(delta.get("delta_id", "") or ""),
            "delta_type": str(delta.get("delta_type", "") or ""),
            "status": cast(Any, _policy_result_status(decision)),
            "commit_policy": cast(Any, decision.get("commit_policy", "blocked") or "blocked"),
            "confidence": float(decision.get("confidence", delta.get("confidence", 0.0)) or 0.0),
            "dry_run": True,
            "requires_user_confirmation": bool(decision.get("requires_user_confirmation", False)),
            "source_slice_id": str(delta.get("source_slice_id", "") or ""),
            "affected_memory_ids": target_memory_ids,
            "validation_errors": list(decision.get("validation_errors", [])),
            "warnings": list(decision.get("warnings", [])),
            "result_message": str(decision.get("result_message", "") or ""),
        })
    return results


def _writeback_summary_from_results(
    results: list[dict[str, Any]],
    *,
    proposed_count: int,
    dry_run: bool,
    status: str,
) -> WritebackSummary:
    return {
        "status": cast(Any, status),
        "dry_run": dry_run,
        "proposed_count": proposed_count,
        "auto_committed_count": sum(1 for result in results if result.get("status") == "committed"),
        "manual_review_count": sum(1 for result in results if result.get("commit_policy") == "manual_review"),
        "blocked_count": sum(1 for result in results if result.get("commit_policy") == "blocked"),
        "rejected_count": sum(1 for result in results if result.get("status") == "rejected"),
    }


def _persist_slice(slice_obj: ActiveMemorySlice) -> None:
    try:
        persist_slice_history(slice_obj)
    except Exception as exc:
        warnings = list(slice_obj.get("warnings", []))
        warnings.append(f"Slice history persistence failed: {type(exc).__name__}.")
        slice_obj["warnings"] = warnings
        log.warning("slice history persistence failed: %s", exc)


def _generate_active_memory_slice_internal(
    query: str,
    *,
    scope_id: str,
    environment: dict[str, Any] | None,
    use_llm: bool,
    writeback_mode: WritebackExecutionMode,
    auto_commit_safe: bool,
    dry_run: bool,
    persist_history: bool,
) -> ActiveMemorySlice:
    env = normalize_runtime_environment(environment)
    normalized_scope = _normalize_scope_id(scope_id)
    previous_slice = _load_previous_slice(env, normalized_scope)
    activation_env = _continuity_environment(env, previous_slice)
    candidate_bundle = generate_candidates(query, normalized_scope, env, use_llm=use_llm)
    _oov_threshold = _memem_settings.MEMEM_RECALL_OOV_THRESHOLD
    if _oov_threshold > 0.0 and _detect_out_of_vault(query, candidate_bundle, _oov_threshold):
        _sid = str(env.get("session_id", "") or "")
        return _make_gating_stub(query, normalized_scope, _sid, "out_of_vault")
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    include_history = bool(env.get("history_mode") or env.get("include_history"))

    pre = apply_pre_boundaries(all_candidates, normalized_scope, include_history=include_history)
    filtered_candidates = pre["candidates"]
    filtered_bundle = _bundle_from_candidates(filtered_candidates, candidate_bundle)

    if use_llm:
        try:
            activation = judge_activation_with_llm(query, normalized_scope, activation_env, filtered_bundle)
        except Exception as exc:
            log.warning("LLM activation failed (%s); falling back to heuristic", exc)
            activation = judge_activation_heuristically(query, normalized_scope, activation_env, filtered_bundle)
            activation["warnings"] = list(activation.get("warnings", [])) + [
                f"LLM activation raised {type(exc).__name__}; used heuristic activation.",
            ]
    else:
        activation = judge_activation_heuristically(query, normalized_scope, activation_env, filtered_bundle)
    if not use_llm:
        activation["warnings"] = list(activation.get("warnings", [])) + ["LLM activation disabled; used heuristic activation."]

    activation["excluded_candidates"] = list(activation.get("excluded_candidates", [])) + pre["excluded_candidates"]
    activation = cast(
        ActivationResult,
        apply_post_boundaries(cast(dict[str, Any], activation), filtered_candidates, normalized_scope, include_history=include_history),
    )
    slice_obj = build_active_memory_slice(query, normalized_scope, env, filtered_bundle, activation)
    carry_forward_limit = int(env.get("continuity_summary_limit", 4) or 4)
    slice_obj = annotate_slice_continuity(slice_obj, previous_slice, carry_forward_limit=carry_forward_limit)
    candidate_deltas = propose_deltas_from_slice(slice_obj)
    policy_decisions = evaluate_delta_proposals(candidate_deltas)
    slice_obj["candidate_deltas"] = candidate_deltas
    slice_obj["delta_results"] = _policy_results(
        cast(list[dict[str, Any]], candidate_deltas),
        cast(list[dict[str, Any]], policy_decisions),
    )
    slice_obj["writeback_summary"] = _writeback_summary_from_results(
        cast(list[dict[str, Any]], slice_obj.get("delta_results", [])),
        proposed_count=len(candidate_deltas),
        dry_run=True,
        status="not_run",
    )

    if writeback_mode != "policy_only" and candidate_deltas:
        public_results = commit_deltas(
            candidate_deltas,
            scope_id=normalized_scope,
            dry_run=dry_run,
            auto_only=auto_commit_safe,
        )
        slice_obj["delta_results"] = cast(list[DeltaWritebackResult], public_results)
        summary = cast(dict[str, Any], public_results[0].get("writeback_summary", {})) if public_results else {}
        if summary:
            slice_obj["writeback_summary"] = cast(WritebackSummary, summary)
        else:
            slice_obj["writeback_summary"] = _writeback_summary_from_results(
                cast(list[dict[str, Any]], public_results),
                proposed_count=len(candidate_deltas),
                dry_run=dry_run,
                status="dry_run" if dry_run else ("committed" if auto_commit_safe else "partial"),
            )
        writeback_warnings: list[str] = []
        for result in public_results:
            writeback_warnings.extend(str(warning) for warning in result.get("warnings", []) if str(warning))
            writeback_warnings.extend(str(error) for error in result.get("validation_errors", []) if str(error))
        if writeback_warnings:
            existing = list(slice_obj.get("warnings", []))
            for warning in writeback_warnings:
                if warning not in existing:
                    existing.append(warning)
            slice_obj["warnings"] = existing

    if persist_history:
        _persist_slice(slice_obj)
    log.info("active_memory_slice_generated %s", json.dumps(summarize_slice_metrics(slice_obj), sort_keys=True))
    return slice_obj


def generate_active_memory_slice_with_writeback(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    auto_commit_safe: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Generate a continuity-aware slice and optionally execute writeback."""
    normalized_scope = _normalize_scope_id(scope_id)
    slice_obj = _generate_active_memory_slice_internal(
        query,
        scope_id=normalized_scope,
        environment=environment,
        use_llm=use_llm,
        writeback_mode="preview" if dry_run else "commit",
        auto_commit_safe=auto_commit_safe,
        dry_run=dry_run,
        persist_history=True,
    )
    return {
        "slice": slice_obj,
        "delta_results": list(slice_obj.get("delta_results", [])),
    }


def _bundle_from_candidates(candidates: list[Candidate], original: CandidateBundle) -> CandidateBundle:
    bundle: CandidateBundle = {
        "current_goal_candidates": [],
        "memory_candidates": [],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }
    for candidate in candidates:
        ctype = candidate.get("candidate_type")
        if ctype == "current_query":
            bundle["current_goal_candidates"].append(candidate)
        elif ctype == "memory":
            bundle["memory_candidates"].append(candidate)
        elif ctype == "playbook":
            bundle["playbook_candidate"] = candidate
        elif ctype == "transcript":
            bundle["transcript_candidates"].append(candidate)
        elif ctype == "environment":
            bundle["environment_candidates"].append(candidate)
        else:
            bundle["artifact_candidates"].append(candidate)
    return bundle


def record_slice_attribution(slice_data: dict, response_text: str) -> None:
    """Compute the 3 signals for every memory in the slice and log to telemetry.

    Called by host integration (hook scripts, MCP wrappers) after the assistant
    has produced a response that referenced this slice. m2 ships this without
    auto-wiring — collection happens lazily as integrations adopt it.
    """
    from memem.attribution import (
        aggregate_signals,
        citation_match,
        embedding_similarity,
        judge_score,
        should_run_judge,
    )
    from memem.telemetry import log_slice_attribution

    slice_id = slice_data.get("slice_id", "")
    items = slice_data.get("items", []) or []
    # Also check pinned/role-categorized fields
    for field in ("goals", "constraints", "active_background", "decisions",
                  "preferences", "failure_patterns"):
        items.extend(slice_data.get(field, []) or [])

    seen_ids: set[str] = set()
    for item in items:
        mem_id = item.get("memory_id") or item.get("id") or ""
        if not mem_id or mem_id in seen_ids:
            continue
        seen_ids.add(mem_id)

        title = item.get("title", "")
        essence = item.get("content", "") or item.get("summary", "") or ""

        emb = embedding_similarity(essence, response_text)
        cite = citation_match(mem_id, title, response_text, memory_essence=essence)
        judge = judge_score(essence, response_text, slice_data.get("query", "")) if should_run_judge() else None  # always None — stub, see judge_score docstring
        agg = aggregate_signals(emb, cite, judge)

        log_slice_attribution(slice_id, mem_id, emb, cite, judge, agg)


def generate_session_start_slice(
    scope_id: str,
    session_id: str,
    memem_dir: str | None = None,
) -> str:
    """Generate a structured 'where we left off' briefing for SessionStart hook injection.

    Sections (in priority order):
      1. Prior working memory  — highest priority; trimmed to ~600 chars/section
      2. Recent decisions       — top 3 with kind='decision' or tag 'decision', last 7 days
      3. Active arcs            — memories with arc_id set and not closed
      4. L0 anchors             — always included via _gather_l0_anchors
      5. Compaction checkpoint  — most recent in last 24h (tagged kind:compaction-checkpoint)

    Char budget: MEMEM_SESSION_START_BUDGET env var, default 2000.
    """
    from pathlib import Path as _Path

    from memem.models import _normalize_scope_id

    budget = _parse_session_start_budget()
    normalized_scope = _normalize_scope_id(scope_id)
    memem_dir_path = _Path(memem_dir) if memem_dir else None

    # -------------------------------------------------------------------
    # Section 1: Prior working memory
    # -------------------------------------------------------------------
    wm_sections = _read_working_memory_for_slice(memem_dir_path)
    wm_block = _render_working_memory_block(wm_sections)

    # -------------------------------------------------------------------
    # Section 1b: Pending instruction rewrite suggestions (M-1 procedural)
    # -------------------------------------------------------------------
    procedural_block = _render_procedural_suggestions_block()

    # -------------------------------------------------------------------
    # Section 1c: Pending contradiction flags (M-3 consolidation)
    # -------------------------------------------------------------------
    contradiction_block = _render_contradiction_flags_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 2: Recent decisions (last 7 days, top 3 by strength)
    # -------------------------------------------------------------------
    decisions_block = _render_decisions_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 3: Active arcs
    # -------------------------------------------------------------------
    arcs_block = _render_active_arcs_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 4: L0 anchors
    # -------------------------------------------------------------------
    l0_block = _render_l0_block(normalized_scope)

    # -------------------------------------------------------------------
    # Section 5: Compaction checkpoint
    # -------------------------------------------------------------------
    checkpoint_block = _render_checkpoint_block(normalized_scope, session_id)

    # -------------------------------------------------------------------
    # Budget enforcement: working_memory is highest priority
    # -------------------------------------------------------------------
    parts = [
        ("working_memory", wm_block),
        ("procedural_suggestions", procedural_block),
        ("contradiction_flags", contradiction_block),
        ("decisions", decisions_block),
        ("arcs", arcs_block),
        ("l0_anchors", l0_block),
        ("checkpoint", checkpoint_block),
    ]

    output_parts: list[str] = []
    chars_used = 0
    sep_len = 2  # len("\n\n") between joined sections

    for section_name, block in parts:
        if not block:
            continue
        # Reserve space for the separator that join() will add before this part
        # (only if there's already content to join after).
        sep_cost = sep_len if output_parts else 0
        block_len = len(block)
        remaining = budget - chars_used - sep_cost
        if section_name == "working_memory":
            # working_memory is never cut unless it alone exceeds budget
            if block_len > budget:
                block = block[:budget]
                block_len = budget
            output_parts.append(block)
            chars_used += block_len + sep_cost
        else:
            if remaining <= 0:
                break
            if block_len > remaining:
                block = block[:remaining]
                block_len = remaining
            output_parts.append(block)
            chars_used += block_len + sep_cost

    return "\n\n".join(p for p in output_parts if p)


def _parse_session_start_budget() -> int:
    """Return char budget for session-start slice from env var (default 2000).

    MEMEM_SESSION_START_BUDGET is preferred. MEMEM_SESSION_START_PROMPT_BUDGET
    is honored as a legacy alias so configs from the pre-m1 hook keep working.
    """
    raw = (
        os.environ.get("MEMEM_SESSION_START_BUDGET")
        or os.environ.get("MEMEM_SESSION_START_PROMPT_BUDGET")
        or "2000"
    ).strip()
    try:
        val = int(raw)
        return max(100, val)
    except ValueError:
        return 2000


def _read_working_memory_for_slice(memem_dir: Any | None) -> dict[str, str]:
    """Read working memory, optionally overriding MEMEM_DIR for test isolation."""
    try:
        import memem.working_memory as wm_mod

        if memem_dir is not None:
            from pathlib import Path as _Path
            wm_file = _Path(memem_dir) / "working_memory.md"
            # Read directly without relying on module-level WORKING_MEMORY_FILE
            if not wm_file.exists():
                return {}
            try:
                text = wm_file.read_text(encoding="utf-8")
                return wm_mod.parse_from_md(text)
            except OSError:
                return {}
        else:
            return wm_mod.read_working_memory()
    except Exception:
        return {}


_WM_SECTION_LABELS = {
    "current_task": "Current task",
    "active_hypothesis": "Active hypothesis",
    "last_3_actions": "Last 3 actions",
    "stuck_on": "Stuck on",
    "decided_this_session": "Decided this session",
}

_WM_SECTION_CAP = 600


def _render_working_memory_block(sections: dict[str, str]) -> str:
    """Render non-empty working memory sections, each capped at _WM_SECTION_CAP chars."""
    if not sections:
        return ""
    parts = ["## Prior working memory"]
    for key, label in _WM_SECTION_LABELS.items():
        body = sections.get(key, "").strip()
        if body:
            if len(body) > _WM_SECTION_CAP:
                body = body[:_WM_SECTION_CAP]
            parts.append(f"**{label}:** {body}")
    if len(parts) == 1:
        return ""
    return "\n\n".join(parts)


def _render_procedural_suggestions_block() -> str:
    """Render pending instruction-rewrite suggestions (kind:procedural-suggestion, status: pending_review).

    Queries all memories with the ``kind:procedural-suggestion`` tag and
    ``status: pending_review``. Sorts by ``created_iso`` ascending (oldest
    first) and surfaces the top 3. Omits the section entirely when none exist.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    pending: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:procedural-suggestion" not in tags:
            continue
        if mem.get("status") != "pending_review":
            continue
        pending.append(mem)

    if not pending:
        return ""

    # Sort oldest-first by created_iso (falls back to created_at)
    def _sort_key(m: dict) -> str:
        return str(m.get("created_iso") or m.get("created_at") or "")

    pending.sort(key=_sort_key)
    top3 = pending[:3]

    lines = [f"## Pending instruction rewrites\n\n{len(pending)} suggestion(s) await your review (oldest first):"]
    for idx, mem in enumerate(top3, start=1):
        created_iso = str(mem.get("created_iso") or mem.get("created_at") or "")
        date_str = created_iso[:10] if created_iso else "unknown date"
        reason = ""
        essence = mem.get("essence") or mem.get("full_record") or ""
        # Extract reason line from body
        for line in essence.splitlines():
            if line.startswith("**Reason:**"):
                reason = line[len("**Reason:**"):].strip()
                break
        # Extract current/proposed from body
        current_snippet = ""
        proposed_snippet = ""
        in_current = False
        in_proposed = False
        for line in essence.splitlines():
            if line.startswith("**Current:**"):
                in_current = True
                in_proposed = False
            elif line.startswith("**Proposed:**"):
                in_current = False
                in_proposed = True
            elif line.strip() == "```":
                pass
            elif in_current and line.strip() and not current_snippet:
                current_snippet = line.strip()[:100]
                in_current = False
            elif in_proposed and line.strip() and not proposed_snippet:
                proposed_snippet = line.strip()[:100]
                in_proposed = False
        entry_lines = [f"\n{idx}. **From session {date_str}:** {reason}"]
        if current_snippet:
            entry_lines.append(f'   - Current: "{current_snippet}"')
        entry_lines.append(f'   - Proposed: "{proposed_snippet}"')
        lines.append("\n".join(entry_lines))

    return "\n".join(lines)


def _render_contradiction_flags_block(normalized_scope: str) -> str:
    """Render pending contradiction flags from M-3 consolidation pass.

    Queries memories tagged ``kind:contradiction-flag`` (NOT deprecated) for
    this scope. Surfaces up to 3 oldest-first so the user reviews and resolves
    them. Omits the section entirely when none exist.
    """
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    flags: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:contradiction-flag" not in tags:
            continue
        if mem.get("status") == "deprecated":
            continue
        # Scope filter: same project, OR project='general' (consolidation-flag is cross-cutting)
        proj = _normalize_scope_id(str(mem.get("project", "general") or "general"))
        if proj != normalized_scope and proj != "general":
            continue
        flags.append(mem)

    if not flags:
        return ""

    def _sort_key(m: dict) -> str:
        return str(m.get("created_iso") or m.get("created_at") or "")

    flags.sort(key=_sort_key)
    top3 = flags[:3]

    lines = [f"## Pending contradiction review\n\n{len(flags)} unresolved contradiction(s) found by consolidation:"]
    for idx, mem in enumerate(top3, start=1):
        created = str(mem.get("created_iso") or mem.get("created_at") or "")
        date_str = created[:10] if created else "unknown date"
        title = str(mem.get("title", "") or "(untitled)")[:80]
        essence = str(mem.get("essence") or mem.get("full_record") or "")[:200]
        lines.append(f"\n{idx}. **{date_str}** — {title}\n   {essence}")

    return "\n".join(lines)


def _render_decisions_block(normalized_scope: str) -> str:
    """Render top-3 decision memories from the last 7 days, ranked by decay strength."""
    from datetime import UTC, datetime, timedelta

    try:
        from memem import decay as _decay_mod
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=7)

    all_mems = _obsidian_memories()
    decision_mems: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        # canonical: bare "decision" tag; "kind:<value>" tag prefix is reserved for compaction-checkpoint and similar
        has_decision = "decision" in tags
        if not has_decision:
            continue
        # Check recency: parse created_at
        created_str = mem.get("created_at", "") or ""
        created_dt = parse_iso_dt(created_str)
        if created_dt is not None and created_dt < cutoff:
            continue
        decision_mems.append(mem)

    # Rank by compute_strength (access_count * recency_decay)
    def _score(mem: dict) -> float:
        try:
            return _decay_mod.compute_strength(mem, now=now)
        except Exception:
            return 0.0

    decision_mems.sort(key=_score, reverse=True)
    top3 = decision_mems[:3]

    if not top3:
        return ""

    lines = ["## Recent decisions"]
    for mem in top3:
        title = mem.get("title", "Untitled")
        essence = (mem.get("essence") or mem.get("full_record", ""))[:300]
        lines.append(f"- **{title}**: {essence}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_active_arcs_block(normalized_scope: str) -> str:
    """Render memories with arc_id set and not closed (no 'closed' tag, no closed=True field)."""
    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    all_mems = _obsidian_memories()
    arc_mems: list[dict[str, Any]] = []
    for mem in all_mems:
        arc_id = mem.get("arc_id") or ""
        if not arc_id:
            continue
        # Skip if closed
        tags = mem.get("domain_tags") or []
        if "closed" in tags:
            continue
        if mem.get("closed") is True or str(mem.get("closed", "")).lower() == "true":
            continue
        arc_mems.append(mem)

    if not arc_mems:
        return ""

    lines = ["## Active arcs"]
    for mem in arc_mems:
        title = mem.get("title", "Untitled")
        content = (mem.get("essence") or mem.get("full_record", ""))[:200]
        arc_id = mem.get("arc_id", "")
        lines.append(f"- **[arc:{arc_id}] {title}**: {content}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_l0_block(normalized_scope: str) -> str:
    """Render L0 anchor memories for the given scope."""
    anchors = _gather_l0_anchors(normalized_scope)
    if not anchors:
        return "## L0 anchors\n\n(none for scope)"

    lines = ["## L0 anchors"]
    for anchor in anchors:
        title = anchor.get("title", "Untitled")
        content = (anchor.get("content", "") or "")[:200]
        lines.append(f"- **{title}**: {content}")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:])


def _render_checkpoint_block(normalized_scope: str, session_id: str) -> str:
    """Render the most recent compaction checkpoint from the last 24h."""
    from datetime import UTC, datetime, timedelta

    try:
        from memem.obsidian_store import _obsidian_memories
    except Exception:
        return ""

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    all_mems = _obsidian_memories()
    checkpoints: list[dict[str, Any]] = []
    for mem in all_mems:
        tags = mem.get("domain_tags") or []
        if "kind:compaction-checkpoint" not in tags:
            continue
        # Check project matches
        from memem.models import _normalize_scope_id as _nsi
        mem_project = _nsi(str(mem.get("project", "general") or "general"))
        if mem_project != normalized_scope and normalized_scope != "general":
            continue
        # Check recency
        created_str = mem.get("created_at", "") or ""
        created_dt = parse_iso_dt(created_str)
        if created_dt is not None and created_dt < cutoff:
            continue
        checkpoints.append(mem)

    if not checkpoints:
        return ""

    # Sort by created_at descending, take most recent
    checkpoints.sort(key=lambda m: m.get("created_at", "") or "", reverse=True)
    latest = checkpoints[0]

    title = latest.get("title", "Compaction checkpoint")
    essence = (latest.get("essence") or latest.get("full_record", ""))[:600]
    return f"## Compaction checkpoint\n\n**{title}**\n\n{essence}"


def active_slice_response(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    raw_json: bool = False,
) -> str:
    slice_obj = generate_active_memory_slice(query, scope_id=scope_id, environment=environment, use_llm=use_llm)
    if raw_json:
        return json.dumps(slice_obj, indent=2, sort_keys=True)
    return generate_prompt_context(
        query,
        scope_id=scope_id,
        environment=environment,
        use_llm=use_llm,
        mode="slice",
        slice_obj=slice_obj,
    )


def generate_prompt_context(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
    mode: PromptContextMode = "slice",
    slice_obj: ActiveMemorySlice | None = None,
) -> str:
    """Generate runtime prompt context using slice-first projection.

    History persistence is the caller's responsibility. The internal slice
    fallback path (when ``slice_obj`` is None) deliberately runs with
    ``persist_history=False`` so that wrappers like
    ``active_slice_response`` — which already persist — don't double-write
    history records for what is logically one turn.
    """
    if slice_obj is None:
        current_slice = _generate_active_memory_slice_internal(
            query,
            scope_id=scope_id,
            environment=environment,
            use_llm=use_llm,
            writeback_mode="policy_only",
            auto_commit_safe=False,
            dry_run=True,
            persist_history=False,
        )
    else:
        current_slice = slice_obj
    if not current_slice.get("should_emit_context", True):
        return ""

    if mode == "slice":
        compact_budget = environment.get("prompt_budget_chars") if isinstance(environment, dict) else None
        if isinstance(compact_budget, int) and compact_budget > 0:
            return render_slice_as_compact_context(current_slice, max_chars=compact_budget)
        return render_slice_as_prompt_context(current_slice)

    if mode != "assembly":
        raise ValueError(f"Unknown prompt context mode: {mode}")

    try:
        from memem.assembly import context_assemble

        assembled = context_assemble(query, project=scope_id)
    except Exception as exc:
        log.warning("assembly projection failed; falling back to slice projection: %s", exc)
        return render_slice_as_prompt_context(current_slice)

    return assembled or render_slice_as_prompt_context(current_slice)
