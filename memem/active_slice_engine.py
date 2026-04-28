"""Active Memory Slice Engine orchestration."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal, cast

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
from memem.models import LAYER_L0, LAYER_L3, _normalize_scope_id
from memem.slice_history import annotate_slice_continuity, load_slice_history, persist_slice_history

log = logging.getLogger("memem-active-slice")

_MAX_MEMORY_CANDIDATES = 20
_MAX_GRAPH_CANDIDATES = 20
_MAX_TRANSCRIPT_CANDIDATES = 5
_MAX_ARTIFACT_CANDIDATES = 8
PromptContextMode = Literal["slice", "assembly"]
WritebackExecutionMode = Literal["policy_only", "preview", "commit"]


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
) -> CandidateBundle:
    """Generate bounded candidate pool for Active Memory Slice activation."""
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
        memory_candidates = [
            normalize_memory_candidate(mem, source_reason="recall", score=0.75 - (idx * 0.02))
            for idx, mem in enumerate(memories[:_MAX_MEMORY_CANDIDATES])
        ]
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

    return {
        "current_goal_candidates": current,
        "memory_candidates": _dedupe_candidates((memory_candidates + graph_candidates)[:_MAX_MEMORY_CANDIDATES + _MAX_GRAPH_CANDIDATES]),
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


def generate_active_memory_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> ActiveMemorySlice:
    """Main Active Memory Slice Engine entrypoint."""
    return _generate_active_memory_slice_internal(
        query,
        scope_id=scope_id,
        environment=environment,
        use_llm=use_llm,
        writeback_mode="policy_only",
        auto_commit_safe=False,
        dry_run=True,
        persist_history=True,
    )


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
    candidate_bundle = generate_candidates(query, normalized_scope, env)
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
