"""Metrics helpers for Active Memory Slice generation."""

from __future__ import annotations

from typing import Any

from memem.active_slice import ActiveMemorySlice


def summarize_slice_metrics(slice_obj: ActiveMemorySlice) -> dict[str, Any]:
    """Return a compact, structured summary of a generated slice."""
    return {
        "slice_id": slice_obj.get("slice_id", ""),
        "scope_id": slice_obj.get("scope_id", "default"),
        "query": slice_obj.get("query", ""),
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
        "excluded_candidates_count": len(slice_obj.get("excluded_candidates", [])),
        "candidate_deltas_count": len(slice_obj.get("candidate_deltas", [])),
        "should_emit_context": bool(slice_obj.get("should_emit_context", False)),
        "confidence": float(slice_obj.get("confidence", 0.0) or 0.0),
        "warnings_count": len(slice_obj.get("warnings", [])),
    }
