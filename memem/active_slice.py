"""Active Memory Slice schemas, builders, and prompt projection helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict

from memem.models import DEFAULT_LAYER, now_iso

CandidateType = Literal["memory", "playbook", "transcript", "artifact", "environment", "current_query"]
ActiveRole = Literal[
    "goal",
    "constraint",
    "background",
    "artifact_context",
    "decision",
    "failure_pattern",
    "open_tension",
    "preference",
    "environment_fact",
    "candidate_delta_context",
]


class Candidate(TypedDict, total=False):
    candidate_id: str
    candidate_type: CandidateType
    memory_id: str
    artifact_id: str
    title: str
    summary: str
    content: str
    source_ref: str
    source_type: str
    project: str
    status: str
    importance: int
    layer: int
    score: float
    source_score: float
    graph_distance: int
    source_reason: str


class ActiveMemoryItem(TypedDict, total=False):
    memory_id: str
    role: ActiveRole
    title: str
    summary: str
    source_type: str
    project: str
    importance: int
    layer: int
    score: float
    why_activated: str


class ActiveArtifact(TypedDict, total=False):
    artifact_id: str
    artifact_type: Literal["playbook", "memory_note", "transcript", "external_file", "repo_file", "draft"]
    title: str
    path: str
    summary: str
    relevance_score: float
    why_activated: str


class ActiveTension(TypedDict, total=False):
    tension_id: str
    description: str
    severity: Literal["low", "medium", "high"]
    linked_memory_ids: list[str]
    why_open: str


class ActiveMemorySlice(TypedDict, total=False):
    slice_id: str
    session_id: str
    scope_id: str
    query: str
    input_goal: str
    generated_at: str
    environment: dict
    goals: list[ActiveMemoryItem]
    constraints: list[ActiveMemoryItem]
    active_background: list[ActiveMemoryItem]
    decisions: list[ActiveMemoryItem]
    preferences: list[ActiveMemoryItem]
    failure_patterns: list[ActiveMemoryItem]
    artifacts: list[ActiveArtifact]
    open_tensions: list[ActiveTension]
    excluded_candidates: list[dict]
    candidate_deltas: list[dict]
    projection_hint: dict
    candidate_count: int
    recall_candidate_count: int
    should_emit_context: bool
    activation_mode: Literal["heuristic", "llm", "hybrid"]
    confidence: float
    warnings: list[str]


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha1(encoded.encode('utf-8')).hexdigest()[:12]}"


def _compact(text: str, limit: int = 500) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]


def current_query_candidate(query: str, scope_id: str) -> Candidate:
    return {
        "candidate_id": "current_query",
        "candidate_type": "current_query",
        "title": "Current user goal",
        "summary": _compact(query, 500),
        "content": query,
        "source_type": "current_query",
        "project": scope_id or "general",
        "status": "active",
        "importance": 5,
        "layer": 0,
        "score": 1.0,
        "source_score": 1.0,
        "source_reason": "The user's current request is always the primary active goal.",
    }


def normalize_memory_candidate(
    mem: dict,
    source_reason: str = "recall",
    score: float | None = None,
    graph_distance: int = 0,
) -> Candidate:
    memory_id = mem.get("id", "")
    essence = mem.get("essence") or mem.get("full_record", "") or ""
    final_score = float(score if score is not None else mem.get("score", 0.5) or 0.5)
    return {
        "candidate_id": f"memory:{memory_id[:8]}",
        "candidate_type": "memory",
        "memory_id": memory_id,
        "title": mem.get("title", "Untitled"),
        "summary": _compact(essence, 500),
        "content": essence,
        "source_ref": mem.get("file", ""),
        "source_type": mem.get("source_type", "memory"),
        "project": mem.get("project", "general"),
        "status": mem.get("status", "active"),
        "importance": int(mem.get("importance", 3) or 3),
        "layer": int(mem.get("layer", DEFAULT_LAYER) or DEFAULT_LAYER),
        "score": final_score,
        "source_score": final_score,
        "graph_distance": graph_distance,
        "source_reason": source_reason,
    }


def normalize_artifact_candidate(
    artifact_type: str,
    title: str,
    content: str,
    path: str = "",
    score: float = 0.5,
    project: str = "general",
) -> Candidate:
    artifact_id = _stable_id("artifact", {"type": artifact_type, "title": title, "path": path, "content": content[:200]})
    return {
        "candidate_id": artifact_id,
        "candidate_type": "playbook" if artifact_type == "playbook" else "artifact",
        "artifact_id": artifact_id,
        "title": title,
        "summary": _compact(content, 500),
        "content": content,
        "source_ref": path,
        "source_type": artifact_type,
        "project": project,
        "status": "active",
        "importance": 3,
        "layer": DEFAULT_LAYER,
        "score": float(score),
        "source_score": float(score),
        "source_reason": f"{artifact_type} candidate",
    }


def normalize_transcript_candidate(text: str, title: str = "Transcript excerpt", score: float = 0.4) -> Candidate:
    candidate_id = _stable_id("transcript", {"title": title, "text": text[:500]})
    return {
        "candidate_id": candidate_id,
        "candidate_type": "transcript",
        "artifact_id": candidate_id,
        "title": title,
        "summary": _compact(text, 500),
        "content": text,
        "source_type": "transcript",
        "project": "general",
        "status": "active",
        "importance": 2,
        "layer": DEFAULT_LAYER,
        "score": float(score),
        "source_score": float(score),
        "source_reason": "transcript search candidate",
    }


def normalize_environment_candidate(key: str, value: Any, score: float = 0.5) -> Candidate:
    summary = f"{key}: {value}"
    candidate_id = _stable_id("environment", {"key": key, "value": value})
    return {
        "candidate_id": candidate_id,
        "candidate_type": "environment",
        "title": key,
        "summary": _compact(summary, 300),
        "content": summary,
        "source_type": "environment",
        "project": "general",
        "status": "active",
        "importance": 3,
        "layer": DEFAULT_LAYER,
        "score": float(score),
        "source_score": float(score),
        "source_reason": "runtime environment",
    }


def flatten_candidate_bundle(candidate_bundle: dict) -> list[Candidate]:
    candidates: list[Candidate] = []
    for key in ("current_goal_candidates", "memory_candidates", "artifact_candidates", "transcript_candidates", "environment_candidates"):
        value = candidate_bundle.get(key, [])
        if isinstance(value, list):
            candidates.extend(value)
    playbook = candidate_bundle.get("playbook_candidate")
    if isinstance(playbook, dict):
        candidates.append(playbook)
    return candidates


def _candidate_lookup(candidate_bundle: dict) -> dict[str, Candidate]:
    lookup: dict[str, Candidate] = {}
    for cand in flatten_candidate_bundle(candidate_bundle):
        cid = cand.get("candidate_id", "")
        if cid:
            lookup[cid] = cand
        mid = cand.get("memory_id", "")
        if mid:
            lookup[mid] = cand
            lookup[mid[:8]] = cand
        aid = cand.get("artifact_id", "")
        if aid:
            lookup[aid] = cand
    return lookup


def _item_from_candidate(cand: Candidate, role: ActiveRole, why: str = "", score: float | None = None) -> ActiveMemoryItem:
    return {
        "memory_id": cand.get("memory_id", ""),
        "role": role,
        "title": cand.get("title", "Untitled"),
        "summary": cand.get("summary") or _compact(cand.get("content", ""), 500),
        "source_type": cand.get("source_type", cand.get("candidate_type", "candidate")),
        "project": cand.get("project", "general"),
        "importance": int(cand.get("importance", 3) or 3),
        "layer": int(cand.get("layer", DEFAULT_LAYER) or DEFAULT_LAYER),
        "score": float(score if score is not None else cand.get("score", 0.5) or 0.5),
        "why_activated": why or cand.get("source_reason", ""),
    }


def _artifact_from_candidate(cand: Candidate, why: str = "", score: float | None = None) -> ActiveArtifact:
    artifact_type = cand.get("candidate_type", "artifact")
    if artifact_type not in {"playbook", "transcript", "artifact"}:
        artifact_type = "memory_note"
    if artifact_type == "artifact":
        artifact_type = cand.get("source_type", "draft")
    if artifact_type not in {"playbook", "memory_note", "transcript", "external_file", "repo_file", "draft"}:
        artifact_type = "draft"
    return {
        "artifact_id": cand.get("artifact_id", cand.get("candidate_id", "")),
        "artifact_type": artifact_type,
        "title": cand.get("title", "Untitled artifact"),
        "path": cand.get("source_ref", ""),
        "summary": cand.get("summary") or _compact(cand.get("content", ""), 500),
        "relevance_score": float(score if score is not None else cand.get("score", 0.5) or 0.5),
        "why_activated": why or cand.get("source_reason", ""),
    }


def build_active_memory_slice(
    query: str,
    scope_id: str,
    environment: dict,
    candidate_bundle: dict,
    activation_result: dict,
) -> ActiveMemorySlice:
    lookup = _candidate_lookup(candidate_bundle)

    def selected(role_key: str, role: ActiveRole) -> list[ActiveMemoryItem]:
        items = []
        for entry in activation_result.get(role_key, []):
            cid = entry.get("candidate_id") or entry.get("memory_id")
            cand = lookup.get(cid, {})
            if cand:
                items.append(_item_from_candidate(cand, role, entry.get("why", ""), entry.get("score")))
        return items

    goals = selected("goals", "goal")
    if not any(item.get("source_type") == "current_query" for item in goals):
        goals.insert(0, _item_from_candidate(current_query_candidate(query, scope_id), "goal", "Current user request", 1.0))
    constraints = selected("constraints", "constraint")
    active_background = selected("background", "background")
    decisions = selected("decisions", "decision")
    preferences = selected("preferences", "preference")
    failure_patterns = selected("failure_patterns", "failure_pattern")

    artifacts = []
    for entry in activation_result.get("artifact_context", []):
        cid = entry.get("candidate_id") or entry.get("artifact_id")
        cand = lookup.get(cid, {})
        if cand:
            artifacts.append(_artifact_from_candidate(cand, entry.get("why", ""), entry.get("score")))

    tensions: list[ActiveTension] = []
    for entry in activation_result.get("open_tensions", []):
        description = entry.get("description", "")
        if not description:
            continue
        tensions.append({
            "tension_id": entry.get("tension_id") or _stable_id("tension", description),
            "description": description,
            "severity": entry.get("severity", "medium") if entry.get("severity") in {"low", "medium", "high"} else "medium",
            "linked_memory_ids": entry.get("linked_memory_ids", []),
            "why_open": entry.get("why_open", entry.get("why", "")),
        })

    selected_memory_items = [
        item
        for section in (goals, constraints, active_background, decisions, preferences, failure_patterns)
        for item in section
        if item.get("memory_id")
    ]
    recall_candidate_count = (
        len(candidate_bundle.get("memory_candidates", []))
        + len(candidate_bundle.get("transcript_candidates", []))
        + len(candidate_bundle.get("artifact_candidates", []))
        + (1 if candidate_bundle.get("playbook_candidate") else 0)
    )
    should_emit_context = bool(selected_memory_items or artifacts or tensions)

    slice_obj: ActiveMemorySlice = {
        "slice_id": _stable_id("slice", {"query": query, "scope": scope_id, "generated_at": now_iso()}),
        "session_id": environment.get("session_id", ""),
        "scope_id": scope_id,
        "query": query,
        "input_goal": query,
        "generated_at": now_iso(),
        "environment": environment,
        "goals": goals,
        "constraints": constraints,
        "active_background": active_background,
        "decisions": decisions,
        "preferences": preferences,
        "failure_patterns": failure_patterns,
        "artifacts": artifacts,
        "open_tensions": tensions,
        "excluded_candidates": activation_result.get("ignored", []) + activation_result.get("excluded_candidates", []),
        "candidate_deltas": activation_result.get("candidate_deltas", []),
        "projection_hint": {
            "preferred_output_mode": "briefing",
            "must_include_constraints_first": True,
            "should_surface_open_tensions": True,
        },
        "candidate_count": len(flatten_candidate_bundle(candidate_bundle)),
        "recall_candidate_count": recall_candidate_count,
        "should_emit_context": should_emit_context,
        "activation_mode": activation_result.get("activation_mode", "heuristic"),
        "confidence": float(activation_result.get("confidence", 0.6)),
        "warnings": activation_result.get("warnings", []),
    }
    return slice_obj


def _render_items(items: list[dict]) -> list[str]:
    lines = []
    for item in items:
        title = item.get("title", "Untitled")
        summary = item.get("summary", "")
        score = item.get("score", item.get("relevance_score", 0.0))
        why = item.get("why_activated", "")
        prefix = f"- {title}"
        if score:
            prefix += f" ({float(score):.2f})"
        if summary:
            prefix += f" — {summary}"
        if why:
            prefix += f" _[{why}]_"
        lines.append(prefix)
    return lines or ["- None"]


def render_slice_as_prompt_context(slice_obj: ActiveMemorySlice) -> str:
    if not slice_obj.get("should_emit_context", True):
        return ""

    sections = [
        ("Goals", slice_obj.get("goals", [])),
        ("Constraints", slice_obj.get("constraints", [])),
        ("Active Background", slice_obj.get("active_background", [])),
        ("Decisions", slice_obj.get("decisions", [])),
        ("Preferences", slice_obj.get("preferences", [])),
        ("Failure Patterns", slice_obj.get("failure_patterns", [])),
    ]
    lines = [
        "# Active Memory Slice",
        "",
        f"- scope: {slice_obj.get('scope_id', 'default')}",
        f"- query: {slice_obj.get('query', '')}",
        f"- activation: {slice_obj.get('activation_mode', 'heuristic')}",
        "",
    ]
    for title, items in sections:
        lines.append(f"## {title}")
        lines.extend(_render_items(items))
        lines.append("")

    lines.append("## Open Tensions")
    tensions = slice_obj.get("open_tensions", [])
    if tensions:
        for tension in tensions:
            lines.append(
                f"- [{tension.get('severity', 'medium')}] {tension.get('description', '')} "
                f"_{tension.get('why_open', '')}_"
            )
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Artifacts")
    lines.extend(_render_items(slice_obj.get("artifacts", [])))
    lines.append("")

    deltas = slice_obj.get("candidate_deltas", [])
    if deltas:
        lines.append("## Candidate Deltas")
        for delta in deltas:
            lines.append(f"- {delta.get('delta_type', 'delta')}: {delta.get('reason', '')}")
        lines.append("")

    warnings = slice_obj.get("warnings", [])
    if warnings:
        lines.append("## Warnings")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines).strip()
