"""Active Memory Slice schemas, builders, and prompt projection helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import memem.settings as _memem_settings
from memem.models import DEFAULT_LAYER, now_iso

if TYPE_CHECKING:
    from memem.delta import DeltaProposal


# ---------------------------------------------------------------------------
# Universal MemoryItem — used by recall pipeline slice outputs
# ---------------------------------------------------------------------------


class MemoryItem(TypedDict, total=False):
    # — Required —
    id: str
    title: str
    content: str
    source_type: Literal["user", "mined", "import", "transcript", "playbook"]

    # — Intrinsic but optional —
    layer: int                # 0-3
    project: str
    importance: int
    tags: list[str]
    created_at: str
    updated_at: str
    related: list[str]

    # — Bi-temporal fields (v2 schema) —
    valid_at: str             # when this fact became true (ISO timestamp)
    invalid_at: str | None    # when superseded; None = currently valid
    replaced_by: str | None   # memory_id of successor; None if still valid

    # — Decay fields (v2 m3 schema) —
    last_accessed_at: str     # bumped on every recall hit
    access_count: int         # incremented on every recall hit
    decay_immune: bool        # opt-out flag; defaults to False

    # — Per-slice extras (set by the recall pipeline) —
    score: float
    snippet: str              # truncated essence for compact display
    parent_id: str            # for timeline graph relationships
    position: int             # for chronological ordering

CandidateType = Literal["memory", "playbook", "transcript", "artifact", "environment", "current_query"]
ArtifactType = Literal["playbook", "memory_note", "transcript", "external_file", "repo_file", "draft"]
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
    artifact_type: ArtifactType
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


class SliceDiff(TypedDict, total=False):
    new_goals: list[str]
    dropped_goals: list[str]
    new_constraints: list[str]
    resolved_constraints: list[str]
    new_tensions: list[str]
    resolved_tensions: list[str]
    new_artifacts: list[str]
    dropped_artifacts: list[str]
    activation_mode_changed: bool
    confidence_delta: float


class ArtifactProgression(TypedDict, total=False):
    stage: Literal["none", "discovered", "drafting", "revising", "review_ready", "stalled"]
    summary: str
    current_artifact_ids: list[str]
    previous_artifact_ids: list[str]
    changed: bool
    signals: list[str]


class DeltaWritebackResult(TypedDict, total=False):
    delta_id: str
    delta_type: str
    status: Literal["not_run", "dry_run", "committed", "rejected", "blocked", "skipped"]
    commit_policy: Literal["auto_safe", "manual_review", "blocked"]
    confidence: float
    dry_run: bool
    requires_user_confirmation: bool
    source_slice_id: str
    affected_memory_ids: list[str]
    validation_errors: list[str]
    warnings: list[str]
    result_message: str


class WritebackSummary(TypedDict, total=False):
    status: Literal["not_run", "dry_run", "committed", "partial", "blocked"]
    dry_run: bool
    proposed_count: int
    auto_committed_count: int
    manual_review_count: int
    blocked_count: int
    rejected_count: int


class ExcludedCandidate(TypedDict, total=False):
    candidate_id: str
    reason: str
    role: str
    kept_candidate_id: str
    drop_reason: str


class ActivationEntry(TypedDict, total=False):
    candidate_id: str
    memory_id: str
    artifact_id: str
    why: str
    score: float
    centrality: float
    role_confidence: float
    drop_reason: str


class ActivationTension(TypedDict, total=False):
    tension_id: str
    description: str
    severity: Literal["low", "medium", "high"]
    linked_memory_ids: list[str]
    why_open: str
    why: str
    centrality: float
    role_confidence: float


class ActivationResult(TypedDict, total=False):
    goals: list[ActivationEntry]
    constraints: list[ActivationEntry]
    background: list[ActivationEntry]
    decisions: list[ActivationEntry]
    preferences: list[ActivationEntry]
    failure_patterns: list[ActivationEntry]
    artifact_context: list[ActivationEntry]
    open_tensions: list[ActivationTension]
    ignored: list[ExcludedCandidate]
    excluded_candidates: list[ExcludedCandidate]
    candidate_deltas: list[DeltaProposal]
    activation_mode: Literal["heuristic", "llm", "hybrid"]
    confidence: float
    warnings: list[str]


class CandidateBundle(TypedDict):
    current_goal_candidates: list[Candidate]
    memory_candidates: list[Candidate]
    playbook_candidate: Candidate | None
    transcript_candidates: list[Candidate]
    artifact_candidates: list[Candidate]
    environment_candidates: list[Candidate]


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
    resolved_tensions: list[ActiveTension]
    excluded_candidates: list[ExcludedCandidate]
    candidate_deltas: list[DeltaProposal]
    delta_results: list[DeltaWritebackResult]
    projection_hint: dict
    previous_slice_id: str
    slice_diff: SliceDiff
    carry_forward_summary: list[str]
    artifact_progression: ArtifactProgression
    task_mode: str
    writeback_summary: WritebackSummary
    candidate_count: int
    recall_candidate_count: int
    should_emit_context: bool
    activation_mode: Literal["heuristic", "llm", "hybrid"]
    confidence: float
    warnings: list[str]
    # ── Universal fields for recall pipeline slices ──
    slice_kind: Literal["active", "search", "get", "timeline", "assembled"]
    items: list[MemoryItem]
    ordering: Literal["relevance", "chronological", "manual"]
    layer_summary: dict[int, int]  # {0: 1, 1: 0, 2: 3, 3: 0} count per layer in items
    # ── Extra fields for get/timeline kinds ──
    missing_ids: list[str]
    linked: list[MemoryItem]
    anchor_id: str
    anchor_title: str
    before_items: list[MemoryItem]
    after_items: list[MemoryItem]
    # ── Extra fields for assembled kind ──
    sub_slices: list[ActiveMemorySlice]
    composition_strategy: str
    # ── v1.9 gating ──
    gating_reason: str | None  # set when gating short-circuits the full pipeline


# Type alias for new code that wants the cleaner name.
MemorySlice = ActiveMemorySlice


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha1(encoded.encode('utf-8')).hexdigest()[:12]}"


def _compact(text: str, limit: int = 500) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return f"{cleaned[: limit - 3].rstrip()}..."


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
        # v1.8.3: layer=0 (L0) is a valid value but falsy — the old
        # `... or DEFAULT_LAYER` reclassified L0 memories as L2. Use an
        # explicit None check so 0 survives.
        "layer": int(mem["layer"]) if mem.get("layer") is not None else DEFAULT_LAYER,
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


def flatten_candidate_bundle(candidate_bundle: CandidateBundle) -> list[Candidate]:
    candidates: list[Candidate] = []
    for key in ("current_goal_candidates", "memory_candidates", "artifact_candidates", "transcript_candidates", "environment_candidates"):
        value = candidate_bundle.get(key, [])
        if isinstance(value, list):
            candidates.extend(value)
    playbook = candidate_bundle.get("playbook_candidate")
    if isinstance(playbook, dict):
        candidates.append(playbook)
    return candidates


def _candidate_lookup(candidate_bundle: CandidateBundle) -> dict[str, Candidate]:
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


def candidate_reference_keys(entry: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("candidate_id", "memory_id", "artifact_id"):
        value = entry.get(key, "")
        if not isinstance(value, str) or not value:
            continue
        refs.append(value)
        if key == "memory_id" and len(value) > 8:
            refs.append(value[:8])
    return refs


def resolve_candidate_reference(entry: Mapping[str, Any], lookup: Mapping[str, Candidate]) -> Candidate | None:
    refs = candidate_reference_keys(entry)
    for ref in refs:
        candidate = lookup.get(ref)
        if candidate:
            return candidate
    return None


def _item_from_candidate(cand: Candidate, role: ActiveRole, why: str = "", score: float | None = None) -> ActiveMemoryItem:
    return {
        "memory_id": cand.get("memory_id", ""),
        "role": role,
        "title": cand.get("title", "Untitled"),
        "summary": cand.get("summary") or _compact(cand.get("content", ""), 500),
        "source_type": cand.get("source_type", cand.get("candidate_type", "candidate")),
        "project": cand.get("project", "general"),
        "importance": int(cand.get("importance", 3) or 3),
        # v1.8.3: layer=0 (L0) survives the None check; old `or` fallback
        # reclassified L0 candidates as L2.
        "layer": int(cand["layer"]) if cand.get("layer") is not None else DEFAULT_LAYER,
        "score": float(score if score is not None else cand.get("score", 0.5) or 0.5),
        "why_activated": why or cand.get("source_reason", ""),
    }


def _artifact_from_candidate(cand: Candidate, why: str = "", score: float | None = None) -> ActiveArtifact:
    artifact_type: str = cand.get("candidate_type", "artifact")
    if artifact_type not in {"playbook", "transcript", "artifact"}:
        artifact_type = "memory_note"
    if artifact_type == "artifact":
        artifact_type = cand.get("source_type", "draft")
    if artifact_type not in {"playbook", "memory_note", "transcript", "external_file", "repo_file", "draft"}:
        artifact_type = "draft"
    return {
        "artifact_id": cand.get("artifact_id", cand.get("candidate_id", "")),
        "artifact_type": cast(ArtifactType, artifact_type),
        "title": cand.get("title", "Untitled artifact"),
        "path": cand.get("source_ref", ""),
        "summary": cand.get("summary") or _compact(cand.get("content", ""), 500),
        "relevance_score": float(score if score is not None else cand.get("score", 0.5) or 0.5),
        "why_activated": why or cand.get("source_reason", ""),
    }


def build_active_memory_slice(
    query: str,
    scope_id: str,
    environment: dict[str, Any],
    candidate_bundle: CandidateBundle,
    activation_result: ActivationResult,
) -> ActiveMemorySlice:
    lookup = _candidate_lookup(candidate_bundle)

    def selected(role_key: str, role: ActiveRole) -> list[ActiveMemoryItem]:
        items: list[ActiveMemoryItem] = []
        entries = cast(list[ActivationEntry], activation_result.get(role_key, []))
        for entry in entries:
            cand = resolve_candidate_reference(entry, lookup)
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

    artifacts: list[ActiveArtifact] = []
    for entry in activation_result.get("artifact_context", []):
        cand = resolve_candidate_reference(entry, lookup)
        if cand:
            artifacts.append(_artifact_from_candidate(cand, entry.get("why", ""), entry.get("score")))

    tensions: list[ActiveTension] = []
    for tension_entry in activation_result.get("open_tensions", []):
        description_value = tension_entry.get("description", "")
        description = description_value if isinstance(description_value, str) else ""
        if not description:
            continue
        severity_value = tension_entry.get("severity", "medium")
        if severity_value in {"low", "medium", "high"}:
            severity = cast(Literal["low", "medium", "high"], severity_value)
        else:
            severity = "medium"
        linked_memory_ids_value = tension_entry.get("linked_memory_ids", [])
        linked_memory_ids = linked_memory_ids_value if isinstance(linked_memory_ids_value, list) else []
        tension_id_value = tension_entry.get("tension_id")
        tension_id = tension_id_value if isinstance(tension_id_value, str) and tension_id_value else _stable_id("tension", description)
        why_open_value = tension_entry.get("why_open", tension_entry.get("why", ""))
        why_open = why_open_value if isinstance(why_open_value, str) else ""
        tensions.append({
            "tension_id": tension_id,
            "description": description,
            "severity": severity,
            "linked_memory_ids": linked_memory_ids,
            "why_open": why_open,
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
    confidence = float(activation_result.get("confidence", 0.6))
    has_recall_candidates = recall_candidate_count > 0
    has_grounding_context = bool(selected_memory_items or artifacts or has_recall_candidates)
    should_emit_context = bool(
        selected_memory_items
        or artifacts
        or (tensions and has_grounding_context)
        or (has_recall_candidates and confidence >= _memem_settings.MEMEM_RECALL_MIN_CONFIDENCE)
    )

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
        "resolved_tensions": [],
        "excluded_candidates": activation_result.get("ignored", []) + activation_result.get("excluded_candidates", []),
        "candidate_deltas": activation_result.get("candidate_deltas", []),
        "delta_results": [],
        "projection_hint": {
            "preferred_output_mode": "slice",
            "must_include_constraints_first": True,
            "should_surface_open_tensions": True,
        },
        "previous_slice_id": "",
        "slice_diff": {},
        "carry_forward_summary": [],
        "artifact_progression": {"stage": "none", "signals": []},
        "task_mode": str(environment.get("task_mode", "") or ""),
        "writeback_summary": {
            "status": "not_run",
            "dry_run": True,
            "proposed_count": len(activation_result.get("candidate_deltas", [])),
            "auto_committed_count": 0,
            "manual_review_count": 0,
            "blocked_count": 0,
            "rejected_count": 0,
        },
        "candidate_count": len(flatten_candidate_bundle(candidate_bundle)),
        "recall_candidate_count": recall_candidate_count,
        "should_emit_context": should_emit_context,
        "gating_reason": "" if should_emit_context else "low_confidence",
        "activation_mode": activation_result.get("activation_mode", "heuristic"),
        "confidence": confidence,
        "warnings": activation_result.get("warnings", []),
        # Universal items[] surface so attribution / dreamer / recall consumers
        # can iterate the memories that landed in this slice without re-walking
        # the section-specific lists. Mirrors the search/get/timeline builders.
        # The active builder produces ActiveMemoryItem (memory_id-keyed) rather
        # than MemoryItem (id-keyed); consumers handle both via item.get("memory_id")
        # or item.get("id"). The cast pacifies mypy without runtime cost.
        "items": cast(list[MemoryItem], selected_memory_items),
        "slice_kind": "active",
    }
    return slice_obj


def _render_items(items: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in items:
        title = _compact(str(item.get("title", "Untitled") or "Untitled"), 90)
        summary = _compact(str(item.get("summary", "") or ""), 220)
        why = _compact(str(item.get("why_activated", "") or ""), 120)
        prefix = f"- {title}"
        if summary and summary.lower() != title.lower():
            prefix += f" — {summary}"
        if why:
            prefix += f" [{why}]"
        normalized = prefix.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(prefix)
    return lines


def _render_tensions(tensions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for tension in tensions:
        description = _compact(str(tension.get("description", "") or ""), 220)
        if not description:
            continue
        severity = str(tension.get("severity", "medium") or "medium").lower()
        why_open = _compact(str(tension.get("why_open", "") or ""), 120)
        line = f"- [{severity}] {description}"
        if why_open and why_open.casefold() not in description.casefold():
            line += f" [{why_open}]"
        normalized = line.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(line)
    return lines


def _render_deltas(deltas: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for delta in deltas:
        delta_type = _compact(str(delta.get("delta_type", "delta") or "delta"), 40)
        reason = _compact(str(delta.get("reason", "") or ""), 220)
        line = f"- {delta_type}"
        if reason:
            line += f": {reason}"
        lines.append(line)
    return lines


def _render_string_list(items: Sequence[Any], limit: int = 220) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _compact(str(item or ""), limit)
        if not text:
            continue
        line = f"- {text}"
        normalized = line.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(line)
    return lines


def _render_writeback(
    summary: Mapping[str, Any] | None,
    results: Sequence[Mapping[str, Any]],
) -> list[str]:
    lines: list[str] = []
    if summary:
        status = str(summary.get("status", "") or "")
        proposed = int(summary.get("proposed_count", 0) or 0)
        interesting = status and status != "not_run"
        interesting = interesting or proposed > 0
        interesting = interesting or any(int(summary.get(key, 0) or 0) for key in ("auto_committed_count", "manual_review_count", "blocked_count", "rejected_count"))
        if interesting:
            bits = [f"status={status}"] if status else []
            bits.append(f"proposed={proposed}")
            bits.append(f"auto={int(summary.get('auto_committed_count', 0) or 0)}")
            bits.append(f"manual={int(summary.get('manual_review_count', 0) or 0)}")
            bits.append(f"blocked={int(summary.get('blocked_count', 0) or 0)}")
            bits.append(f"rejected={int(summary.get('rejected_count', 0) or 0)}")
            if "dry_run" in summary:
                bits.append(f"dry_run={bool(summary.get('dry_run', False))}")
            lines.append(f"- {'; '.join(bits)}")
    for result in results:
        delta_type = _compact(str(result.get("delta_type", "delta") or "delta"), 40)
        status = _compact(str(result.get("status", "unknown") or "unknown"), 24)
        policy = _compact(str(result.get("commit_policy", "") or ""), 24)
        message = _compact(str(result.get("result_message", "") or ""), 120)
        errors = [str(error) for error in result.get("validation_errors", []) if error]
        line = f"- {status} {delta_type}"
        if policy:
            line += f" ({policy})"
        if message:
            line += f": {message}"
        elif errors:
            line += f": {_compact('; '.join(errors), 120)}"
        lines.append(line)
    return lines


def _render_section(title: str, lines: Sequence[str]) -> str:
    if not lines:
        return ""
    return "\n".join([f"## {title}", *lines])


def _slice_header(slice_obj: ActiveMemorySlice) -> str:
    confidence_value = slice_obj.get("confidence", 0.0)
    confidence = float(confidence_value) if isinstance(confidence_value, int | float) else 0.0
    header_lines = [
        "# Active Memory Slice",
        "",
        f"- scope: {slice_obj.get('scope_id', 'default')}",
        f"- query: {_compact(str(slice_obj.get('query', '') or ''), 240)}",
        f"- activation: {slice_obj.get('activation_mode', 'heuristic')}",
        f"- confidence: {confidence:.2f}",
    ]
    task_mode = _compact(str(slice_obj.get("task_mode", "") or ""), 40)
    if task_mode:
        header_lines.append(f"- task mode: {task_mode}")
    previous_slice_id = _compact(str(slice_obj.get("previous_slice_id", "") or ""), 80)
    if previous_slice_id:
        header_lines.append(f"- previous slice: {previous_slice_id}")
    return "\n".join(header_lines)


def _render_slice(slice_obj: ActiveMemorySlice, max_chars: int | None = None) -> str:
    if not slice_obj.get("should_emit_context", True):
        return ""

    sections = [
        _slice_header(slice_obj),
        _render_section("Goals", _render_items(slice_obj.get("goals", []))),
        _render_section("Constraints", _render_items(slice_obj.get("constraints", []))),
        _render_section("Decisions", _render_items(slice_obj.get("decisions", []))),
        _render_section("Failure Patterns", _render_items(slice_obj.get("failure_patterns", []))),
        _render_section("Open Tensions", _render_tensions(slice_obj.get("open_tensions", []))),
        _render_section("Resolved Tensions", _render_tensions(slice_obj.get("resolved_tensions", []))),
        _render_section("Carry Forward", _render_string_list(slice_obj.get("carry_forward_summary", []))),
        _render_section("Artifacts", _render_items(slice_obj.get("artifacts", []))),
        _render_section("Preferences", _render_items(slice_obj.get("preferences", []))),
        _render_section("Active Background", _render_items(slice_obj.get("active_background", []))),
        _render_section("Candidate Deltas", _render_deltas(slice_obj.get("candidate_deltas", []))),
        _render_section(
            "Writeback",
            _render_writeback(
                cast(Mapping[str, Any] | None, slice_obj.get("writeback_summary")),
                cast(Sequence[Mapping[str, Any]], slice_obj.get("delta_results", [])),
            ),
        ),
        _render_section("Warnings", [f"- {_compact(str(warning), 220)}" for warning in slice_obj.get("warnings", [])]),
    ]
    blocks = [section for section in sections if section]
    rendered = "\n\n".join(blocks).strip()
    if max_chars is None or len(rendered) <= max_chars:
        return rendered

    limited_blocks: list[str] = []
    current_length = 0
    for block in blocks:
        addition = len(block) + (2 if limited_blocks else 0)
        if limited_blocks and current_length + addition > max_chars:
            break
        if not limited_blocks and len(block) > max_chars:
            return _compact(block, max_chars)
        limited_blocks.append(block)
        current_length += addition

    truncated = "\n\n".join(limited_blocks).strip()
    if len(truncated) < len(rendered):
        suffix = "\n\n- Context truncated for budget."
        if len(truncated) + len(suffix) <= max_chars:
            truncated += suffix
    return truncated


def render_slice_as_prompt_context(slice_obj: ActiveMemorySlice) -> str:
    return _render_slice(slice_obj)


def render_slice_as_compact_context(slice_obj: ActiveMemorySlice, max_chars: int = 4000) -> str:
    return _render_slice(slice_obj, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Universal slice renderers for recall pipeline
# ---------------------------------------------------------------------------


def _format_compact_index_line_from_item(item: MemoryItem) -> str:
    """Produce the standard compact line format from a MemoryItem.

    Format: ``[<8-char-id>] L<layer> <title> — <snippet>``
    Matches _format_compact_index_line in recall.py: title is NOT truncated
    here (titles can be up to 120 chars per _parse_obsidian_memory_file);
    only the snippet is clipped at 80 chars.
    """
    mem_id = item.get("id", "")[:8]
    layer = item.get("layer", DEFAULT_LAYER)
    title = item.get("title", "Untitled")
    snippet = item.get("snippet", "") or item.get("content", "")[:80]
    snippet = snippet.replace("\n", " ").strip()[:80]
    return f"[{mem_id}] L{layer} {title} — {snippet}"


def _layer_summary_from_items(items: list[MemoryItem]) -> dict[int, int]:
    """Count items per layer. Returns {layer: count} for all layers present."""
    counts: dict[int, int] = {}
    for item in items:
        raw = item.get("layer")
        layer = int(raw) if raw is not None else DEFAULT_LAYER
        counts[layer] = counts.get(layer, 0) + 1
    return counts


def _render_search_slice(slice_data: ActiveMemorySlice) -> str:
    """Render a search result slice preserving the existing compact index format.

    Keeps backward-compat strings: "### Compact memory index", "memory_get(ids=".
    Adds optional layer summary line.
    """
    items = slice_data.get("items", [])
    linked = slice_data.get("linked", [])
    layer_summary = slice_data.get("layer_summary", {})

    lines = [f"### Compact memory index ({len(items)} results)"]
    if layer_summary:
        layer_str = ", ".join(f"L{k}={v}" for k, v in sorted(layer_summary.items()) if v)
        if layer_str:
            lines.append(f"_layers: {layer_str}_")
    for item in items:
        lines.append(_format_compact_index_line_from_item(item))

    if linked:
        lines.append("")
        lines.append(f"### Related memories (via graph traversal, {len(linked)} linked)")
        for item in linked:
            lines.append(_format_compact_index_line_from_item(item))

    lines.append("")
    lines.append("_Use memory_get(ids=[...]) to fetch full content for any of these._")
    return "\n".join(lines)


def _render_get_slice(slice_data: ActiveMemorySlice) -> str:
    """Render a get-kind slice: full content per item + linked related section.

    Preserves the ### [id] title format and [not-found: id] markers
    used by existing tests.
    """
    items = slice_data.get("items", [])
    missing_ids = slice_data.get("missing_ids", [])
    linked = slice_data.get("linked", [])

    lines: list[str] = []
    for item in items:
        mid = item.get("id", "")[:8]
        layer = item.get("layer", DEFAULT_LAYER)
        title = item.get("title", "Untitled")
        project = item.get("project", "general")
        source_type = item.get("source_type", "unknown")
        tags = item.get("tags", [])
        related = item.get("related", [])
        content = item.get("content", "")

        item_lines = [
            f"### [{mid}] {title}",
            f"- **layer:** L{layer}",
            f"- **project:** {project}",
            f"- **source:** {source_type}",
        ]
        if tags:
            item_lines.append(f"- **tags:** {', '.join(tags)}")
        if related:
            item_lines.append(f"- **related:** {', '.join(r[:8] for r in related)}")
        item_lines.append("")
        item_lines.append(content)
        item_lines.append("")
        lines.append("\n".join(item_lines))

    for mid in missing_ids:
        lines.append(f"[not-found: {mid}]")

    if linked:
        lines.append(f"### Related memories (via graph traversal, {len(linked)} linked)")
        for item in linked:
            lines.append(_format_compact_index_line_from_item(item))

    return "\n".join(lines)


def _render_timeline_slice(slice_data: ActiveMemorySlice) -> str:
    """Render a timeline-kind slice: chronological ordering with before/after sections.

    Preserves the format of the original memory_timeline output.
    """
    anchor_id = slice_data.get("anchor_id", "")[:8]
    anchor_title = slice_data.get("anchor_title", "Untitled")
    before_items = slice_data.get("before_items", [])
    anchor_item = None
    after_items = slice_data.get("after_items", [])
    # The anchor is stored as the first item in items if present
    all_items = slice_data.get("items", [])
    for item in all_items:
        if item.get("id", "")[:8] == anchor_id:
            anchor_item = item
            break

    lines = [f"### Timeline around [{anchor_id}] {anchor_title}", ""]
    if before_items:
        lines.append(f"**Before ({len(before_items)}):**")
        for item in before_items:
            ts = item.get("created_at", "")[:10]
            lines.append(f"- {ts}  {_format_compact_index_line_from_item(item)}")
        lines.append("")
    lines.append("**Anchor:**")
    anchor_ts = ""
    if anchor_item:
        anchor_ts = anchor_item.get("created_at", "")[:10]
        anchor_line = _format_compact_index_line_from_item(anchor_item)
    else:
        anchor_line = f"[{anchor_id}] {anchor_title}"
    lines.append(f"- {anchor_ts}  {anchor_line}")
    lines.append("")
    if after_items:
        lines.append(f"**After ({len(after_items)}):**")
        for item in after_items:
            ts = item.get("created_at", "")[:10]
            lines.append(f"- {ts}  {_format_compact_index_line_from_item(item)}")
        lines.append("")
    return "\n".join(lines)


def _render_assembled_slice(slice_data: ActiveMemorySlice) -> str:
    """Render an assembled briefing slice (composed from sub-slices via context_assemble)."""
    items = slice_data.get("items", [])
    layer_summary = slice_data.get("layer_summary", {})
    sub_slices = slice_data.get("sub_slices", [])
    composition = slice_data.get("composition_strategy", "")

    lines = ["## Composed Briefing"]
    if composition:
        lines.append(f"_strategy: {composition}, sources: {len(sub_slices)} sub-slices_")
    if layer_summary:
        layer_str = ", ".join(f"L{k}={v}" for k, v in sorted(layer_summary.items()) if v)
        if layer_str:
            lines.append(f"_layers: {layer_str}_")
    lines.append("")

    for item in items:
        lines.append(_format_compact_index_line_from_item(item))

    return "\n".join(lines)


def render_slice_markdown(slice_data: ActiveMemorySlice) -> str:
    """Universal markdown renderer for any MemorySlice variant.

    Branches on slice_kind to pick the right section structure:
    - active:    use existing render_slice_as_prompt_context
    - search:    compact index + optional layer summary
    - get:       full content per item (### [id] title format)
    - timeline:  chronologically ordered items with before/after sections
    - assembled: composed briefing (stub; m4 will refine)
    """
    kind = slice_data.get("slice_kind", "active")
    if kind == "active":
        return render_slice_as_prompt_context(slice_data)
    if kind == "search":
        return _render_search_slice(slice_data)
    if kind == "get":
        return _render_get_slice(slice_data)
    if kind == "timeline":
        return _render_timeline_slice(slice_data)
    if kind == "assembled":
        return _render_assembled_slice(slice_data)
    return render_slice_as_prompt_context(slice_data)  # fallback
