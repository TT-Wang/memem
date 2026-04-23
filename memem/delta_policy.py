"""Deterministic policy evaluation for proposed memory deltas."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from memem.models import _normalize_scope_id
from memem.obsidian_store import _find_best_match, _find_memory
from memem.security import scan_memory_content

if TYPE_CHECKING:
    from memem.delta import DeltaProposal


PolicyDecision = Literal["reject", "review", "commit_safe"]
CommitPolicy = Literal["auto_safe", "manual_review", "blocked"]

_SUPPORTED_DELTA_TYPES = {
    "save_new_memory",
    "deprecate_memory",
    "add_related_link",
    "add_open_tension_memory",
}
_BASE_SCORES = {
    "save_new_memory": 0.3,
    "deprecate_memory": 0.18,
    "add_related_link": 0.42,
    "add_open_tension_memory": 0.25,
}
_SAFE_LINK_CONFIDENCE = 0.72
_DUPLICATE_REJECT_THRESHOLD = 0.7
_DUPLICATE_WARN_THRESHOLD = 0.45


class DeltaPolicyDecision(TypedDict, total=False):
    delta_id: str
    delta_type: str
    decision: PolicyDecision
    commit_policy: CommitPolicy
    score: float
    confidence: float
    requires_user_confirmation: bool
    source_slice_id: str
    scope_id: str
    target_memory_ids: list[str]
    affected_memory_ids: list[str]
    validation_errors: list[str]
    warnings: list[str]
    explanation: list[str]
    result_message: str


def _normalized_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for item in value:
        memory_id = str(item or "").strip()
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        normalized.append(memory_id)
    return normalized


def _confidence(delta: DeltaProposal) -> float:
    raw = delta.get("confidence", 0.0)
    try:
        confidence = float(raw)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(confidence, 1.0))


def _scope_id(delta: DeltaProposal) -> str:
    scope_id = str(delta.get("scope_id", "") or "").strip()
    if scope_id:
        return _normalize_scope_id(scope_id)
    evidence = delta.get("evidence", {})
    if isinstance(evidence, dict):
        return _normalize_scope_id(str(evidence.get("scope_id", "") or evidence.get("project", "") or "general"))
    return _normalize_scope_id("general")


def _memory_project(memory: dict[str, Any]) -> str:
    return _normalize_scope_id(str(memory.get("project", "general") or "general"))


def _target_memories(
    target_memory_ids: list[str],
    scope_id: str,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[tuple[str, str]]]:
    memories: list[dict[str, Any]] = []
    missing: list[str] = []
    deprecated: list[str] = []
    cross_scope: list[tuple[str, str]] = []
    normalized_scope = _normalize_scope_id(scope_id)
    for memory_id in target_memory_ids:
        memory = _find_memory(memory_id)
        if not memory:
            missing.append(memory_id)
            continue
        memories.append(memory)
        if memory.get("status") == "deprecated":
            deprecated.append(memory.get("id", memory_id)[:8])
        project = _memory_project(memory)
        if project != normalized_scope:
            cross_scope.append((memory.get("id", memory_id)[:8], project))
    return memories, missing, deprecated, cross_scope


def _append_target_validation_errors(
    *,
    scope_id: str,
    missing: list[str],
    deprecated: list[str],
    cross_scope: list[tuple[str, str]],
    validation_errors: list[str],
) -> None:
    if missing:
        validation_errors.extend(f"Target memory not found: {memory_id}" for memory_id in missing)
    if deprecated:
        validation_errors.extend(f"Target memory is deprecated: {memory_id}" for memory_id in deprecated)
    if cross_scope:
        validation_errors.extend(
            f"Target memory is outside writeback scope {scope_id}: {memory_id} (project={project})"
            for memory_id, project in cross_scope
        )


def _duplicate_signal(delta: DeltaProposal, explanation: list[str], warnings: list[str], validation_errors: list[str]) -> None:
    content = str(delta.get("proposed_content", "") or "").strip()
    if not content:
        return
    matched, score = _find_best_match(content, _scope_id(delta))
    matched_id = ""
    if matched:
        matched_id = str(matched.get("id", ""))[:8]
    explanation.append(f"Duplicate check score {score:.2f}.")
    if score >= _DUPLICATE_REJECT_THRESHOLD:
        validation_errors.append(
            f"Proposed content is too close to existing memory {matched_id} (score {score:.2f})."
        )
    elif score >= _DUPLICATE_WARN_THRESHOLD:
        warnings.append(
            f"Proposed content is similar to existing memory {matched_id} (score {score:.2f})."
        )


def _validate_new_memory(delta: DeltaProposal, explanation: list[str], warnings: list[str], validation_errors: list[str]) -> float:
    score = 0.0
    title = str(delta.get("proposed_title", "") or "").strip()
    content = str(delta.get("proposed_content", "") or "").strip()

    if title:
        score += 0.1
        explanation.append("Proposed title present (+0.10).")
    else:
        validation_errors.append("Proposed title is required.")

    stripped = content.strip(". ")
    if len(stripped) >= 10:
        score += 0.12
        explanation.append("Proposed content is long enough for a durable note (+0.12).")
    else:
        validation_errors.append("Proposed content is too short for a durable memory.")

    if title:
        title_threat = scan_memory_content(title)
        if title_threat:
            validation_errors.append(title_threat)
    if content:
        content_threat = scan_memory_content(content)
        if content_threat:
            validation_errors.append(content_threat)

    target_memory_ids = _normalized_ids(delta.get("target_memory_ids"))
    if target_memory_ids:
        _, missing, deprecated, cross_scope = _target_memories(target_memory_ids, _scope_id(delta))
        _append_target_validation_errors(
            scope_id=_scope_id(delta),
            missing=missing,
            deprecated=deprecated,
            cross_scope=cross_scope,
            validation_errors=validation_errors,
        )
        if not missing and not deprecated and not cross_scope:
            score += 0.05
            explanation.append("All referenced target memories resolve cleanly (+0.05).")

    _duplicate_signal(delta, explanation, warnings, validation_errors)
    return score


def _classify_related_link(
    delta: DeltaProposal,
    score: float,
    explanation: list[str],
    warnings: list[str],
    validation_errors: list[str],
) -> tuple[PolicyDecision, CommitPolicy, float]:
    target_memory_ids = _normalized_ids(delta.get("target_memory_ids"))
    if len(target_memory_ids) < 2:
        validation_errors.append("Related links require exactly two target memories.")
    elif len(target_memory_ids) > 2:
        warnings.append("Only the first two distinct target memories are eligible for linking.")
        target_memory_ids = target_memory_ids[:2]

    scope_id = _scope_id(delta)
    memories, missing, deprecated, cross_scope = _target_memories(target_memory_ids[:2], scope_id)
    _append_target_validation_errors(
        scope_id=scope_id,
        missing=missing,
        deprecated=deprecated,
        cross_scope=cross_scope,
        validation_errors=validation_errors,
    )

    if len(memories) == 2 and not missing and not deprecated and not cross_scope:
        score += 0.2
        explanation.append("Both target memories exist and are active (+0.20).")
        first, second = memories
        first_related = {str(memory_id)[:8] for memory_id in first.get("related", [])}
        second_related = {str(memory_id)[:8] for memory_id in second.get("related", [])}
        if second.get("id", "")[:8] in first_related and first.get("id", "")[:8] in second_related:
            warnings.append("Target memories are already linked; commit would be a no-op.")
            score -= 0.03
            explanation.append("Existing symmetric link detected (-0.03).")

    if validation_errors:
        return "reject", "blocked", score
    if delta.get("requires_user_confirmation"):
        warnings.append("Proposal is marked as requiring explicit user confirmation.")
        return "review", "manual_review", score
    if _confidence(delta) < _SAFE_LINK_CONFIDENCE:
        warnings.append(
            f"Confidence {_confidence(delta):.2f} is below the auto-safe threshold {_SAFE_LINK_CONFIDENCE:.2f}."
        )
        return "review", "manual_review", score
    if len(target_memory_ids) != 2:
        return "review", "manual_review", score
    return "commit_safe", "auto_safe", min(score + 0.15, 1.0)


def _result_message(decision: PolicyDecision, validation_errors: list[str], warnings: list[str]) -> str:
    if validation_errors:
        return validation_errors[0]
    if decision == "commit_safe":
        return "Eligible for explicit commit without additional review."
    if warnings:
        return warnings[0]
    return "Manual review required by policy."


def evaluate_delta_proposal(delta: DeltaProposal) -> DeltaPolicyDecision:
    """Score and classify a delta proposal using deterministic policy rules."""
    delta_type = str(delta.get("delta_type", "") or "").strip()
    validation_errors: list[str] = []
    warnings: list[str] = []
    explanation: list[str] = []
    target_memory_ids = _normalized_ids(delta.get("target_memory_ids"))
    confidence = _confidence(delta)

    if delta_type not in _SUPPORTED_DELTA_TYPES:
        validation_errors.append(f"Unsupported delta type: {delta_type or '<missing>'}")

    base_score = _BASE_SCORES.get(delta_type, 0.0)
    score = base_score + (confidence * 0.25)
    explanation.append(f"Base score {base_score:.2f} for {delta_type or 'unknown'} delta.")
    explanation.append(f"Confidence contribution {confidence * 0.25:.2f} from {confidence:.2f}.")

    if delta_type == "add_related_link":
        decision, commit_policy, score = _classify_related_link(
            delta,
            score,
            explanation,
            warnings,
            validation_errors,
        )
    elif delta_type in {"save_new_memory", "add_open_tension_memory"}:
        score += _validate_new_memory(delta, explanation, warnings, validation_errors)
        if delta_type == "add_open_tension_memory":
            warnings.append("Open-tension notes stay in manual review because they represent unresolved work.")
        if delta.get("requires_user_confirmation"):
            warnings.append("Proposal is marked as requiring explicit user confirmation.")
        if validation_errors:
            decision = "reject"
            commit_policy = "blocked"
        else:
            explanation.append("New note creation is held for manual review even when well-formed.")
            decision = "review"
            commit_policy = "manual_review"
    else:
        scope_id = _scope_id(delta)
        target_memories, missing, deprecated, cross_scope = _target_memories(target_memory_ids[:1], scope_id)
        if not target_memory_ids:
            validation_errors.append("Deprecation requires a target memory.")
        _append_target_validation_errors(
            scope_id=scope_id,
            missing=missing,
            deprecated=deprecated,
            cross_scope=cross_scope,
            validation_errors=validation_errors,
        )
        if target_memories and target_memories[0].get("status") == "deprecated":
            validation_errors.append("Target memory is already deprecated.")
        if validation_errors:
            decision = "reject"
            commit_policy = "blocked"
        else:
            explanation.append("Deprecation remains manual-review only because it is destructive.")
            score += 0.12
            decision = "review"
            commit_policy = "manual_review"

    return {
        "delta_id": str(delta.get("delta_id", "") or ""),
        "delta_type": delta_type,
        "decision": decision,
        "commit_policy": commit_policy,
        "score": round(max(0.0, min(score, 1.0)), 3),
        "confidence": confidence,
        "requires_user_confirmation": bool(delta.get("requires_user_confirmation", False)),
        "source_slice_id": str(delta.get("source_slice_id", "") or ""),
        "scope_id": _scope_id(delta),
        "target_memory_ids": target_memory_ids,
        "affected_memory_ids": [memory_id[:8] for memory_id in target_memory_ids],
        "validation_errors": validation_errors,
        "warnings": warnings,
        "explanation": explanation,
        "result_message": _result_message(decision, validation_errors, warnings),
    }


def evaluate_delta_proposals(deltas: list[DeltaProposal]) -> list[DeltaPolicyDecision]:
    """Evaluate a list of deltas in a stable, deterministic order."""
    return [evaluate_delta_proposal(delta) for delta in deltas]
