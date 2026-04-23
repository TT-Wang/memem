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
from memem.environment_context import (
    environment_candidates_from_environment,
    normalize_runtime_environment,
)
from memem.models import _normalize_scope_id

log = logging.getLogger("memem-active-slice")

_MAX_MEMORY_CANDIDATES = 20
_MAX_GRAPH_CANDIDATES = 20
_MAX_TRANSCRIPT_CANDIDATES = 5
_MAX_ARTIFACT_CANDIDATES = 8
PromptContextMode = Literal["slice", "assembly"]


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

    memory_candidates: list[Candidate] = []
    try:
        from memem.recall import _search_memories
        memories = _search_memories(
            query,
            scope_id=normalized_scope,
            limit=min(limit, _MAX_MEMORY_CANDIDATES),
            record_access=False,
            expand_links=False,
        )
        memory_candidates = [
            normalize_memory_candidate(mem, source_reason="recall", score=0.75 - (idx * 0.02))
            for idx, mem in enumerate(memories[:_MAX_MEMORY_CANDIDATES])
        ]
    except Exception as exc:
        log.debug("recall candidate generation failed: %s", exc)

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


def generate_active_memory_slice(
    query: str,
    scope_id: str = "default",
    environment: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> ActiveMemorySlice:
    """Main Active Memory Slice Engine entrypoint."""
    env = normalize_runtime_environment(environment)
    normalized_scope = _normalize_scope_id(scope_id)
    candidate_bundle = generate_candidates(query, normalized_scope, env)
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    include_history = bool(env.get("history_mode") or env.get("include_history"))

    pre = apply_pre_boundaries(all_candidates, normalized_scope, include_history=include_history)
    filtered_candidates = pre["candidates"]
    filtered_bundle = _bundle_from_candidates(filtered_candidates, candidate_bundle)

    if use_llm:
        try:
            activation = judge_activation_with_llm(query, normalized_scope, env, filtered_bundle)
        except Exception as exc:
            activation = judge_activation_heuristically(query, normalized_scope, env, filtered_bundle)
            activation["warnings"] = list(activation.get("warnings", [])) + [
                f"LLM activation raised {type(exc).__name__}; used heuristic activation.",
            ]
    else:
        activation = judge_activation_heuristically(query, normalized_scope, env, filtered_bundle)
    if not use_llm:
        activation["warnings"] = list(activation.get("warnings", [])) + ["LLM activation disabled; used heuristic activation."]

    activation["excluded_candidates"] = list(activation.get("excluded_candidates", [])) + pre["excluded_candidates"]
    activation = cast(
        ActivationResult,
        apply_post_boundaries(cast(dict[str, Any], activation), filtered_candidates, normalized_scope, include_history=include_history),
    )
    slice_obj = build_active_memory_slice(query, normalized_scope, env, filtered_bundle, activation)
    slice_obj["candidate_deltas"] = propose_deltas_from_slice(slice_obj)
    log.info("active_memory_slice_generated %s", json.dumps(summarize_slice_metrics(slice_obj), sort_keys=True))
    return slice_obj


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
    """Generate runtime prompt context using slice-first projection."""
    current_slice = slice_obj or generate_active_memory_slice(
        query,
        scope_id=scope_id,
        environment=environment,
        use_llm=use_llm,
    )
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
