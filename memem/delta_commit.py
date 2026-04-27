"""Dry-run-first delta execution against markdown-backed memory notes."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

try:
    import fcntl  # POSIX-only; memem targets macOS/Linux
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

from memem.active_slice import DeltaWritebackResult, WritebackSummary
from memem.delta_policy import DeltaPolicyDecision, evaluate_delta_proposal
from memem.models import DELTA_AUDIT_LOG, DELTA_STATE_DIR, _normalize_scope_id, now_iso
from memem.obsidian_store import (
    _add_related_link,
    _deprecate_memory,
    _find_memory,
    _make_memory,
    _save_memory,
)

if TYPE_CHECKING:
    from memem.delta import DeltaProposal


ResultStatus = Literal["not_run", "dry_run", "committed", "rejected", "blocked", "skipped"]
SummaryStatus = Literal["not_run", "dry_run", "committed", "partial", "blocked"]


class DeltaExecutionPreview(TypedDict, total=False):
    delta_id: str
    delta_type: str
    action: str
    target_memory_ids: list[str]
    title: str
    project: str
    content_excerpt: str
    already_linked: bool
    memory_titles: list[str]


class DeltaWritebackBatch(TypedDict, total=False):
    execution_id: str
    dry_run: bool
    results: list[DeltaWritebackResult]
    previews: list[DeltaExecutionPreview]
    writeback_summary: WritebackSummary
    audit_log_path: str
    state_file: str
    warnings: list[str]


def _approved_ids(approved_delta_ids: Collection[str] | None) -> set[str]:
    if not approved_delta_ids:
        return set()
    return {str(delta_id) for delta_id in approved_delta_ids if str(delta_id)}


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


def _content_excerpt(content: str) -> str:
    normalized = " ".join(content.split())
    return normalized[:160]


def _write_json_file(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or None, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_delta_audit_entry(entry: dict[str, Any]) -> None:
    """Append one JSON-line to the audit log, locked exclusively.

    Concurrent writers (hook subprocess + MCP tool call) would otherwise
    interleave bytes mid-line, breaking the JSONL invariant readers rely on.
    """
    DELTA_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, default=str) + "\n"
    with open(DELTA_AUDIT_LOG, "a", encoding="utf-8") as handle:
        if _HAS_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            handle.write(line)


def _preview_related_link(delta: DeltaProposal, target_memory_ids: list[str]) -> DeltaExecutionPreview:
    memory_titles: list[str] = []
    already_linked = False
    first = _find_memory(target_memory_ids[0]) if len(target_memory_ids) >= 1 else None
    second = _find_memory(target_memory_ids[1]) if len(target_memory_ids) >= 2 else None
    if first:
        memory_titles.append(str(first.get("title", first.get("id", ""))))
    if second:
        memory_titles.append(str(second.get("title", second.get("id", ""))))
    if first and second:
        first_related = {str(memory_id)[:8] for memory_id in first.get("related", [])}
        second_related = {str(memory_id)[:8] for memory_id in second.get("related", [])}
        already_linked = second.get("id", "")[:8] in first_related and first.get("id", "")[:8] in second_related
    return {
        "delta_id": str(delta.get("delta_id", "") or ""),
        "delta_type": str(delta.get("delta_type", "") or ""),
        "action": "add_related_link",
        "target_memory_ids": [memory_id[:8] for memory_id in target_memory_ids[:2]],
        "memory_titles": memory_titles,
        "already_linked": already_linked,
    }


def _preview_new_memory(delta: DeltaProposal, target_memory_ids: list[str], action: str) -> DeltaExecutionPreview:
    return {
        "delta_id": str(delta.get("delta_id", "") or ""),
        "delta_type": str(delta.get("delta_type", "") or ""),
        "action": action,
        "target_memory_ids": [memory_id[:8] for memory_id in target_memory_ids],
        "title": str(delta.get("proposed_title", "") or ""),
        "project": _normalize_scope_id(str(delta.get("scope_id", "") or "general")),
        "content_excerpt": _content_excerpt(str(delta.get("proposed_content", "") or "")),
    }


def _preview_deprecation(delta: DeltaProposal, target_memory_ids: list[str]) -> DeltaExecutionPreview:
    title = ""
    if target_memory_ids:
        memory = _find_memory(target_memory_ids[0])
        if memory:
            title = str(memory.get("title", ""))
    return {
        "delta_id": str(delta.get("delta_id", "") or ""),
        "delta_type": str(delta.get("delta_type", "") or ""),
        "action": "deprecate_memory",
        "target_memory_ids": [memory_id[:8] for memory_id in target_memory_ids[:1]],
        "title": title,
    }


def _build_preview(delta: DeltaProposal, decision: DeltaPolicyDecision) -> DeltaExecutionPreview:
    target_memory_ids = _normalized_ids(decision.get("target_memory_ids", delta.get("target_memory_ids")))
    delta_type = str(delta.get("delta_type", "") or "")
    if delta_type == "add_related_link":
        return _preview_related_link(delta, target_memory_ids)
    if delta_type in {"save_new_memory", "add_open_tension_memory"}:
        return _preview_new_memory(delta, target_memory_ids, delta_type)
    return _preview_deprecation(delta, target_memory_ids)


def _result(
    delta: DeltaProposal,
    decision: DeltaPolicyDecision,
    *,
    status: ResultStatus,
    dry_run: bool,
    affected_memory_ids: list[str],
    warnings: list[str],
    validation_errors: list[str],
    result_message: str,
) -> DeltaWritebackResult:
    return {
        "delta_id": str(delta.get("delta_id", "") or ""),
        "delta_type": str(delta.get("delta_type", "") or ""),
        "status": status,
        "commit_policy": cast(
            Literal["auto_safe", "manual_review", "blocked"],
            decision.get("commit_policy", "blocked") or "blocked",
        ),
        "confidence": float(decision.get("confidence", 0.0) or 0.0),
        "dry_run": dry_run,
        "requires_user_confirmation": bool(decision.get("requires_user_confirmation", False)),
        "source_slice_id": str(delta.get("source_slice_id", "") or ""),
        "affected_memory_ids": [memory_id[:8] for memory_id in affected_memory_ids],
        "validation_errors": validation_errors,
        "warnings": warnings,
        "result_message": result_message,
    }


def _append_warning(result: DeltaWritebackResult, message: str) -> None:
    warnings = list(result.get("warnings", []))
    if message not in warnings:
        warnings.append(message)
    result["warnings"] = warnings


def _decision_changed(previous: DeltaPolicyDecision, current: DeltaPolicyDecision) -> bool:
    keys = (
        "decision",
        "commit_policy",
        "target_memory_ids",
        "validation_errors",
        "warnings",
    )
    return any(previous.get(key) != current.get(key) for key in keys)


def _commit_time_decision(
    delta: DeltaProposal,
    provided_decision: DeltaPolicyDecision | None,
) -> DeltaPolicyDecision:
    """Re-evaluate at commit time only when no fresh decision was provided.

    The batch path passes the same decision it already evaluated upstream;
    re-running ``evaluate_delta_proposal`` here would double the FTS
    duplicate-check cost on every save_new_memory delta. Trust the caller
    when a decision is supplied, and only re-run when none is given.
    """
    if provided_decision is not None:
        return provided_decision
    return evaluate_delta_proposal(delta)


def _project_for_delta(delta: DeltaProposal, decision: DeltaPolicyDecision) -> str:
    scope_id = str(decision.get("scope_id", "") or delta.get("scope_id", "") or "").strip()
    return _normalize_scope_id(scope_id or "general")


def _tags_for_delta(delta: DeltaProposal) -> list[str]:
    tags = delta.get("proposed_tags", [])
    if not isinstance(tags, list):
        return []
    return [str(tag).strip() for tag in tags if str(tag).strip()]


def _importance_for_delta(delta: DeltaProposal) -> int:
    raw = delta.get("proposed_importance", 3)
    try:
        importance = int(raw)
    except (TypeError, ValueError):
        importance = 3
    return max(1, min(importance, 5))


def _commit_related_link(
    target_memory_ids: list[str],
    dry_run: bool,
) -> tuple[ResultStatus, list[str], str, list[str]]:
    affected = [memory_id[:8] for memory_id in target_memory_ids[:2]]
    if dry_run:
        return "dry_run", affected, f"Dry run only: would link {', '.join(affected)}.", []
    if len(target_memory_ids) < 2:
        return "blocked", affected, "Related-link commit requires two target memories.", []

    link_status = _add_related_link(target_memory_ids[0], target_memory_ids[1])
    if link_status == "linked":
        return "committed", affected, f"Linked {affected[0]} <-> {affected[1]}.", []
    if link_status == "already_linked":
        return "skipped", affected, "Related link already existed; no note changes were needed.", []
    return "blocked", affected, "Target memory became invalid before the related link could be written.", []


def _commit_new_memory(
    delta: DeltaProposal,
    decision: DeltaPolicyDecision,
    *,
    dry_run: bool,
) -> tuple[ResultStatus, list[str], str, list[str]]:
    preview = _preview_new_memory(
        delta,
        _normalized_ids(decision.get("target_memory_ids", delta.get("target_memory_ids"))),
        str(delta.get("delta_type", "") or "save_new_memory"),
    )
    if dry_run:
        return "dry_run", preview.get("target_memory_ids", []), f"Dry run only: would create '{preview.get('title', '')}'.", []

    memory = _make_memory(
        content=str(delta.get("proposed_content", "") or ""),
        title=str(delta.get("proposed_title", "") or ""),
        tags=_tags_for_delta(delta),
        project=_project_for_delta(delta, decision),
        source_type="user",
        source_session=str(delta.get("source_slice_id", "") or ""),
        importance=_importance_for_delta(delta),
    )
    _save_memory(memory)

    warnings: list[str] = []
    affected_memory_ids = [memory["id"][:8]]
    for target_memory_id in _normalized_ids(decision.get("target_memory_ids", delta.get("target_memory_ids"))):
        affected_memory_ids.append(target_memory_id[:8])
        link_status = _add_related_link(memory["id"], target_memory_id)
        if link_status == "invalid_target":
            warnings.append(
                f"Related-link target became invalid after memory {memory['id'][:8]} was created: {target_memory_id[:8]}."
            )

    result_message = f"Created memory {memory['id'][:8]}."
    if warnings:
        result_message = f"Created memory {memory['id'][:8]}; some follow-up related links could not be written."
    return "committed", affected_memory_ids, result_message, warnings


def _commit_deprecation(
    delta: DeltaProposal,
    decision: DeltaPolicyDecision,
    *,
    dry_run: bool,
) -> tuple[ResultStatus, list[str], str, list[str]]:
    target_memory_ids = _normalized_ids(decision.get("target_memory_ids", delta.get("target_memory_ids")))
    affected = [memory_id[:8] for memory_id in target_memory_ids[:1]]
    if dry_run:
        return "dry_run", affected, f"Dry run only: would deprecate {', '.join(affected) or 'target memory'}.", []
    if not target_memory_ids:
        return "blocked", affected, "Deprecation requires a target memory.", []

    changed = _deprecate_memory(target_memory_ids[0], reason=str(delta.get("reason", "superseded") or "superseded"))
    if changed:
        return "committed", affected, f"Deprecated {affected[0]}.", []
    return "blocked", affected, "Target memory became invalid before deprecation could be written.", []


def _execute_delta(
    delta: DeltaProposal,
    decision: DeltaPolicyDecision,
    *,
    dry_run: bool,
    approved_delta_ids: set[str],
) -> tuple[DeltaWritebackResult, DeltaExecutionPreview]:
    preview = _build_preview(delta, decision)
    validation_errors = list(decision.get("validation_errors", []))
    warnings = list(decision.get("warnings", []))
    decision_name = str(decision.get("decision", "reject") or "reject")
    delta_id = str(delta.get("delta_id", "") or "")

    if decision_name == "reject":
        return _result(
            delta,
            decision,
            status="rejected",
            dry_run=dry_run,
            affected_memory_ids=preview.get("target_memory_ids", []),
            warnings=warnings,
            validation_errors=validation_errors,
            result_message=str(decision.get("result_message", "Rejected by policy.")),
        ), preview

    if not dry_run and decision_name == "review" and delta_id not in approved_delta_ids:
        warnings.append("Manual-review delta was not included in approved_delta_ids.")
        return _result(
            delta,
            decision,
            status="blocked",
            dry_run=False,
            affected_memory_ids=preview.get("target_memory_ids", []),
            warnings=warnings,
            validation_errors=validation_errors,
            result_message="Manual review required before commit.",
        ), preview

    delta_type = str(delta.get("delta_type", "") or "")
    if delta_type == "add_related_link":
        status, affected_memory_ids, result_message, commit_warnings = _commit_related_link(
            _normalized_ids(decision.get("target_memory_ids", delta.get("target_memory_ids"))),
            dry_run,
        )
    elif delta_type in {"save_new_memory", "add_open_tension_memory"}:
        status, affected_memory_ids, result_message, commit_warnings = _commit_new_memory(
            delta,
            decision,
            dry_run=dry_run,
        )
    else:
        status, affected_memory_ids, result_message, commit_warnings = _commit_deprecation(
            delta,
            decision,
            dry_run=dry_run,
        )

    warnings.extend(commit_warnings)
    return _result(
        delta,
        decision,
        status=status,
        dry_run=dry_run,
        affected_memory_ids=affected_memory_ids,
        warnings=warnings,
        validation_errors=validation_errors,
        result_message=result_message,
    ), preview


def _writeback_status(
    results: list[DeltaWritebackResult],
    dry_run: bool,
    *,
    persistence_failed: bool = False,
) -> SummaryStatus:
    if persistence_failed:
        # A dry-run that fails persistence still touched no vault state.
        # Returning "partial" here would mislead callers into thinking
        # writes happened. Check dry_run before any other branch.
        if dry_run:
            return "dry_run"
        if any(result.get("status") == "committed" for result in results):
            return "partial"
        if any(result.get("status") in {"blocked", "rejected"} for result in results):
            return "blocked"
        return "partial"
    if dry_run:
        return "dry_run"
    has_committed = any(result.get("status") == "committed" for result in results)
    has_blocked = any(result.get("status") in {"blocked", "rejected"} for result in results)
    if has_committed and has_blocked:
        return "partial"
    if has_blocked:
        return "blocked"
    return "committed"


def _summarize_results(
    results: list[DeltaWritebackResult],
    dry_run: bool,
    *,
    persistence_failed: bool = False,
) -> WritebackSummary:
    return {
        "status": _writeback_status(results, dry_run, persistence_failed=persistence_failed),
        "dry_run": dry_run,
        "proposed_count": len(results),
        "auto_committed_count": sum(
            1
            for result in results
            if result.get("status") == "committed" and result.get("commit_policy") == "auto_safe"
        ),
        "manual_review_count": sum(
            1
            for result in results
            if result.get("commit_policy") == "manual_review" and result.get("status") != "committed"
        ),
        "blocked_count": sum(1 for result in results if result.get("status") == "blocked"),
        "rejected_count": sum(1 for result in results if result.get("status") == "rejected"),
    }


def _summary_with_counts(
    *,
    status: SummaryStatus,
    dry_run: bool,
    proposed_count: int,
    auto_committed_count: int = 0,
    manual_review_count: int = 0,
    blocked_count: int = 0,
    rejected_count: int = 0,
) -> WritebackSummary:
    return {
        "status": status,
        "dry_run": dry_run,
        "proposed_count": proposed_count,
        "auto_committed_count": auto_committed_count,
        "manual_review_count": manual_review_count,
        "blocked_count": blocked_count,
        "rejected_count": rejected_count,
    }


def _state_payload(
    execution_id: str,
    dry_run: bool,
    results: list[DeltaWritebackResult],
    previews: list[DeltaExecutionPreview],
    summary: WritebackSummary,
    *,
    phase: str,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "generated_at": now_iso(),
        "phase": phase,
        "dry_run": dry_run,
        "results": results,
        "previews": previews,
        "writeback_summary": summary,
    }


def _state_file_path(execution_id: str) -> str:
    DELTA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return str(DELTA_STATE_DIR / f"{execution_id}.json")


def _persist_execution_state(
    state_file: str,
    execution_id: str,
    dry_run: bool,
    results: list[DeltaWritebackResult],
    previews: list[DeltaExecutionPreview],
    summary: WritebackSummary,
    *,
    phase: str,
) -> None:
    _write_json_file(
        state_file,
        _state_payload(execution_id, dry_run, results, previews, summary, phase=phase),
    )


def _persistence_error_message(exc: OSError) -> str:
    return f"Writeback persistence failure: {exc}"


def _blocked_batch(
    deltas: list[DeltaProposal],
    *,
    execution_id: str,
    dry_run: bool,
    message: str,
    state_file: str,
) -> DeltaWritebackBatch:
    results: list[DeltaWritebackResult] = []
    previews: list[DeltaExecutionPreview] = []
    for delta in deltas:
        decision = evaluate_delta_proposal(delta)
        results.append({
            "delta_id": str(delta.get("delta_id", "") or ""),
            "delta_type": str(delta.get("delta_type", "") or ""),
            "status": "blocked",
            "commit_policy": "blocked",
            "confidence": float(decision.get("confidence", 0.0) or 0.0),
            "dry_run": dry_run,
            "requires_user_confirmation": bool(decision.get("requires_user_confirmation", False)),
            "source_slice_id": str(delta.get("source_slice_id", "") or ""),
            "affected_memory_ids": [memory_id[:8] for memory_id in _normalized_ids(delta.get("target_memory_ids"))],
            "validation_errors": [message],
            "warnings": [],
            "result_message": message,
        })
        previews.append(_build_preview(delta, decision))

    summary = _summary_with_counts(
        status="blocked",
        dry_run=dry_run,
        proposed_count=len(deltas),
        blocked_count=len(deltas),
    )
    if state_file:
        try:
            _persist_execution_state(
                state_file,
                execution_id,
                dry_run,
                results,
                previews,
                summary,
                phase="blocked",
            )
        except OSError:
            pass
    return {
        "execution_id": execution_id,
        "dry_run": dry_run,
        "results": results,
        "previews": previews,
        "writeback_summary": summary,
        "audit_log_path": str(DELTA_AUDIT_LOG),
        "state_file": state_file,
        "warnings": [message],
    }


def execute_delta_writeback(
    deltas: list[DeltaProposal],
    *,
    dry_run: bool = True,
    approved_delta_ids: Collection[str] | None = None,
    policy_decisions: list[DeltaPolicyDecision] | None = None,
) -> DeltaWritebackBatch:
    """Execute policy-approved deltas against markdown notes, dry-run first."""
    execution_id = f"writeback-{now_iso().replace(':', '').replace('+', '_')}"
    approved = _approved_ids(approved_delta_ids)
    decision_map = {
        str(decision.get("delta_id", "") or ""): decision
        for decision in (policy_decisions or [])
        if str(decision.get("delta_id", "") or "")
    }

    state_file = _state_file_path(execution_id)
    try:
        _persist_execution_state(
            state_file,
            execution_id,
            dry_run,
            [],
            [],
            _summary_with_counts(status="not_run", dry_run=dry_run, proposed_count=len(deltas)),
            phase="started",
        )
        _append_delta_audit_entry({
            "timestamp": now_iso(),
            "execution_id": execution_id,
            "dry_run": dry_run,
            "event": "execution_started",
            "proposed_count": len(deltas),
        })
    except OSError as exc:
        return _blocked_batch(
            deltas,
            execution_id=execution_id,
            dry_run=dry_run,
            message=_persistence_error_message(exc),
            state_file=state_file,
        )

    results: list[DeltaWritebackResult] = []
    previews: list[DeltaExecutionPreview] = []
    batch_warnings: list[str] = []
    persistence_failed = False
    for delta in deltas:
        delta_id = str(delta.get("delta_id", "") or "")
        decision = _commit_time_decision(delta, decision_map.get(delta_id))
        result, preview = _execute_delta(delta, decision, dry_run=dry_run, approved_delta_ids=approved)
        results.append(result)
        previews.append(preview)

        try:
            _append_delta_audit_entry({
                "timestamp": now_iso(),
                "execution_id": execution_id,
                "dry_run": dry_run,
                "event": "delta_result",
                "preview": preview,
                **decision,
                **result,
            })
        except OSError as exc:
            warning = _persistence_error_message(exc)
            _append_warning(result, warning)
            if warning not in batch_warnings:
                batch_warnings.append(warning)
            persistence_failed = True

        try:
            _persist_execution_state(
                state_file,
                execution_id,
                dry_run,
                results,
                previews,
                _summarize_results(results, dry_run, persistence_failed=persistence_failed),
                phase="in_progress",
            )
        except OSError as exc:
            warning = _persistence_error_message(exc)
            _append_warning(result, warning)
            if warning not in batch_warnings:
                batch_warnings.append(warning)
            persistence_failed = True

    summary = _summarize_results(results, dry_run, persistence_failed=persistence_failed)
    try:
        _persist_execution_state(
            state_file,
            execution_id,
            dry_run,
            results,
            previews,
            summary,
            phase="complete",
        )
    except OSError as exc:
        warning = _persistence_error_message(exc)
        if warning not in batch_warnings:
            batch_warnings.append(warning)
        if results:
            _append_warning(results[-1], warning)
        persistence_failed = True
        summary = _summarize_results(results, dry_run, persistence_failed=True)

    return {
        "execution_id": execution_id,
        "dry_run": dry_run,
        "results": results,
        "previews": previews,
        "writeback_summary": summary,
        "audit_log_path": str(DELTA_AUDIT_LOG),
        "state_file": state_file,
        "warnings": batch_warnings,
    }


def _scoped_delta(delta: DeltaProposal, scope_id: str) -> DeltaProposal:
    scoped = dict(delta)
    if scope_id not in {"", "default"}:
        scoped["scope_id"] = _normalize_scope_id(scope_id)
    elif scoped.get("scope_id"):
        scoped["scope_id"] = _normalize_scope_id(str(scoped.get("scope_id", "") or "general"))
    else:
        scoped["scope_id"] = "general"
    return cast("DeltaProposal", scoped)


def _public_result(
    result: DeltaWritebackResult,
    *,
    preview: DeltaExecutionPreview | None = None,
    execution_id: str = "",
    audit_log_path: str = "",
    state_file: str = "",
    writeback_summary: WritebackSummary | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(result)
    payload["preview"] = dict(preview or {})
    payload["execution_id"] = execution_id
    payload["audit_log_path"] = audit_log_path
    payload["state_file"] = state_file
    payload["writeback_summary"] = dict(writeback_summary or {})
    return payload


def _auto_only_result(
    delta: DeltaProposal,
    decision: DeltaPolicyDecision,
    *,
    dry_run: bool,
    preview: DeltaExecutionPreview,
) -> dict[str, Any]:
    warnings = list(decision.get("warnings", []))
    validation_errors = list(decision.get("validation_errors", []))
    decision_name = str(decision.get("decision", "reject") or "reject")
    if decision_name == "reject":
        result = _result(
            delta,
            decision,
            status="rejected",
            dry_run=dry_run,
            affected_memory_ids=preview.get("target_memory_ids", []),
            warnings=warnings,
            validation_errors=validation_errors,
            result_message=str(decision.get("result_message", "Rejected by policy.")),
        )
    else:
        warning = "Skipped because auto_only=True only commits auto-safe deltas."
        warnings.append(warning)
        result = _result(
            delta,
            decision,
            status="skipped",
            dry_run=dry_run,
            affected_memory_ids=preview.get("target_memory_ids", []),
            warnings=warnings,
            validation_errors=validation_errors,
            result_message=warning,
        )
    return _public_result(result, preview=preview)


def commit_deltas(
    deltas: list[DeltaProposal],
    scope_id: str = "default",
    dry_run: bool = False,
    auto_only: bool = False,
) -> list[dict[str, Any]]:
    """Public wrapper for committing multiple deltas."""
    scoped_deltas = [_scoped_delta(delta, scope_id) for delta in deltas]
    decisions = [evaluate_delta_proposal(delta) for delta in scoped_deltas]

    if auto_only and not dry_run:
        # Position-keyed bookkeeping: callers can theoretically supply two
        # deltas with the same (or empty) delta_id. Indexing by position
        # avoids a silent overwrite where the second delta's result would
        # replace the first's in the returned list.
        eligible: list[DeltaProposal] = []
        eligible_decisions: list[DeltaPolicyDecision] = []
        eligible_positions: list[int] = []
        ordered: list[dict[str, Any] | None] = [None] * len(scoped_deltas)
        for pos, (delta, decision) in enumerate(zip(scoped_deltas, decisions, strict=False)):
            preview = _build_preview(delta, decision)
            if decision.get("commit_policy") == "auto_safe" and decision.get("decision") == "commit_safe":
                eligible.append(delta)
                eligible_decisions.append(decision)
                eligible_positions.append(pos)
                continue
            ordered[pos] = _auto_only_result(delta, decision, dry_run=dry_run, preview=preview)

        if eligible:
            batch = execute_delta_writeback(
                eligible,
                dry_run=dry_run,
                policy_decisions=eligible_decisions,
            )
            previews = batch.get("previews", []) or []
            results = batch.get("results", []) or []
            for offset, result in enumerate(results):
                preview = previews[offset] if offset < len(previews) else None
                ordered[eligible_positions[offset]] = _public_result(
                    result,
                    preview=preview,
                    execution_id=str(batch.get("execution_id", "") or ""),
                    audit_log_path=str(batch.get("audit_log_path", "") or ""),
                    state_file=str(batch.get("state_file", "") or ""),
                    writeback_summary=batch.get("writeback_summary", {}),
                )
        return [entry for entry in ordered if entry is not None]

    batch = execute_delta_writeback(
        scoped_deltas,
        dry_run=dry_run,
        policy_decisions=decisions,
    )
    preview_by_id = {
        str(preview.get("delta_id", "") or ""): preview
        for preview in batch.get("previews", [])
        if str(preview.get("delta_id", "") or "")
    }
    return [
        _public_result(
            result,
            preview=preview_by_id.get(str(result.get("delta_id", "") or "")),
            execution_id=str(batch.get("execution_id", "") or ""),
            audit_log_path=str(batch.get("audit_log_path", "") or ""),
            state_file=str(batch.get("state_file", "") or ""),
            writeback_summary=batch.get("writeback_summary", {}),
        )
        for result in batch.get("results", [])
    ]


def commit_delta(
    delta: DeltaProposal,
    scope_id: str = "default",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Public wrapper for committing a single delta."""
    results = commit_deltas([delta], scope_id=scope_id, dry_run=dry_run)
    return results[0] if results else {}
