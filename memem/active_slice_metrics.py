"""Metrics helpers for Active Memory Slice generation."""

from __future__ import annotations

from typing import Any

from memem.active_slice import ActiveMemorySlice


def summarize_slice_metrics(slice_obj: ActiveMemorySlice) -> dict[str, Any]:
    """Return a compact, structured summary of a generated slice."""
    writeback_summary = slice_obj.get("writeback_summary", {})
    artifact_progression = slice_obj.get("artifact_progression", {})
    slice_diff = slice_obj.get("slice_diff", {})
    return {
        "slice_id": slice_obj.get("slice_id", ""),
        "previous_slice_id": slice_obj.get("previous_slice_id", ""),
        "scope_id": slice_obj.get("scope_id", "default"),
        "query": slice_obj.get("query", ""),
        "task_mode": slice_obj.get("task_mode", ""),
        "activation_mode": slice_obj.get("activation_mode", "heuristic"),
        "candidate_count": int(slice_obj.get("candidate_count", 0) or 0),
        "recall_candidate_count": int(slice_obj.get("recall_candidate_count", 0) or 0),
        "goals_count": len(slice_obj.get("goals", [])),
        "constraints_count": len(slice_obj.get("constraints", [])),
        "active_background_count": len(slice_obj.get("active_background", [])),
        "decisions_count": len(slice_obj.get("decisions", [])),
        "preferences_count": len(slice_obj.get("preferences", [])),
        "failure_patterns_count": len(slice_obj.get("failure_patterns", [])),
        "artifacts_count": len(slice_obj.get("artifacts", [])),
        "open_tensions_count": len(slice_obj.get("open_tensions", [])),
        "resolved_tensions_count": len(slice_obj.get("resolved_tensions", [])),
        "carry_forward_summary_count": len(slice_obj.get("carry_forward_summary", [])),
        "excluded_candidates_count": len(slice_obj.get("excluded_candidates", [])),
        "candidate_deltas_count": len(slice_obj.get("candidate_deltas", [])),
        "delta_results_count": len(slice_obj.get("delta_results", [])),
        "slice_diff_keys": len(slice_diff) if isinstance(slice_diff, dict) else 0,
        "artifact_progression_stage": artifact_progression.get("stage", "") if isinstance(artifact_progression, dict) else "",
        "writeback_status": writeback_summary.get("status", "") if isinstance(writeback_summary, dict) else "",
        "writeback_manual_review_count": int(writeback_summary.get("manual_review_count", 0) or 0) if isinstance(writeback_summary, dict) else 0,
        "writeback_blocked_count": int(writeback_summary.get("blocked_count", 0) or 0) if isinstance(writeback_summary, dict) else 0,
        "should_emit_context": bool(slice_obj.get("should_emit_context", False)),
        "confidence": float(slice_obj.get("confidence", 0.0) or 0.0),
        "warnings_count": len(slice_obj.get("warnings", [])),
    }
