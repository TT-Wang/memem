"""Active Memory Slice Engine orchestration."""

from __future__ import annotations

import json
import logging
import os

from memem.activation import judge_activation_heuristically, judge_activation_with_llm
from memem.active_slice import (
    build_active_memory_slice,
    current_query_candidate,
    flatten_candidate_bundle,
    normalize_artifact_candidate,
    normalize_environment_candidate,
    normalize_memory_candidate,
    normalize_transcript_candidate,
    render_slice_as_prompt_context,
)
from memem.boundaries import apply_post_boundaries, apply_pre_boundaries
from memem.delta import propose_deltas_from_slice
from memem.models import PLAYBOOK_DIR, _normalize_scope_id

log = logging.getLogger("memem-active-slice")

_MAX_MEMORY_CANDIDATES = 20
_MAX_GRAPH_CANDIDATES = 20
_MAX_TRANSCRIPT_CANDIDATES = 5
_MAX_ARTIFACT_CANDIDATES = 8


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for candidate in candidates:
        key = candidate.get("memory_id") or candidate.get("artifact_id") or candidate.get("candidate_id", "")
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _playbook_candidate(scope_id: str) -> dict | None:
    normalized = _normalize_scope_id(scope_id)
    path = PLAYBOOK_DIR / f"{normalized}.md"
    if not path.exists():
        return None
    try:
        content = path.read_text(errors="ignore").strip()
    except OSError:
        return None
    if not content:
        return None
    return normalize_artifact_candidate("playbook", f"{normalized} playbook", content[:4000], path=str(path), score=0.72, project=normalized)


def _transcript_candidates(query: str) -> list[dict]:
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


def _graph_candidates(memory_candidates: list[dict]) -> list[dict]:
    graph: list[dict] = []
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
    environment: dict | None = None,
    limit: int = 20,
) -> dict:
    """Generate bounded candidate pool for Active Memory Slice activation."""
    env = environment or {}
    normalized_scope = _normalize_scope_id(scope_id)
    current = [current_query_candidate(query, normalized_scope)]

    memory_candidates: list[dict] = []
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
    transcript_flag = str(env.get("include_transcripts", os.environ.get("MEMEM_ACTIVE_SLICE_TRANSCRIPTS", "0"))).lower()
    transcript_candidates = (
        _transcript_candidates(query)
        if transcript_flag in {"1", "true", "yes", "on"}
        else []
    )

    artifact_candidates = []
    if playbook:
        artifact_candidates.append(playbook)

    environment_candidates = [
        normalize_environment_candidate(key, value, score=0.5)
        for key, value in sorted(env.items())[:10]
        if key != "session_id"
    ]

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
    environment: dict | None = None,
    use_llm: bool = True,
) -> dict:
    """Main Active Memory Slice Engine entrypoint."""
    env = dict(environment or {})
    normalized_scope = _normalize_scope_id(scope_id)
    candidate_bundle = generate_candidates(query, normalized_scope, env)
    all_candidates = flatten_candidate_bundle(candidate_bundle)
    include_history = bool(env.get("history_mode") or env.get("include_history"))

    pre = apply_pre_boundaries(all_candidates, normalized_scope, include_history=include_history)
    filtered_candidates = pre["candidates"]
    filtered_bundle = _bundle_from_candidates(filtered_candidates, candidate_bundle)

    activation = (
        judge_activation_with_llm(query, normalized_scope, env, filtered_bundle)
        if use_llm else
        judge_activation_heuristically(query, normalized_scope, env, filtered_bundle)
    )
    if not use_llm:
        activation["warnings"] = list(activation.get("warnings", [])) + ["LLM activation disabled; used heuristic activation."]

    activation["excluded_candidates"] = list(activation.get("excluded_candidates", [])) + pre["excluded_candidates"]
    activation = apply_post_boundaries(activation, filtered_candidates, normalized_scope, include_history=include_history)
    slice_obj = build_active_memory_slice(query, normalized_scope, env, filtered_bundle, activation)
    slice_obj["candidate_deltas"] = propose_deltas_from_slice(slice_obj)
    return slice_obj


def _bundle_from_candidates(candidates: list[dict], original: dict) -> dict:
    bundle = {
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
    if bundle["playbook_candidate"] is None and original.get("playbook_candidate"):
        # Preserve absence after boundaries; do not re-add filtered playbook.
        pass
    return bundle


def active_slice_response(
    query: str,
    scope_id: str = "default",
    environment: dict | None = None,
    use_llm: bool = True,
    raw_json: bool = False,
) -> str:
    slice_obj = generate_active_memory_slice(query, scope_id=scope_id, environment=environment, use_llm=use_llm)
    if raw_json:
        return json.dumps(slice_obj, indent=2, sort_keys=True)
    return render_slice_as_prompt_context(slice_obj)
