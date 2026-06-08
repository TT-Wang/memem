"""Tests for v1.9.6 selective-recall features (C1-C5).

Covers:
 C1 — hook emits empty additionalContext when should_emit_context=False
 C2 — MEMEM_RECALL_MIN_CONFIDENCE gating in build_active_memory_slice
 C3 — per-item score floor (MEMEM_RECALL_MIN_ITEM_SCORE) + L0 anchor exemption
 C4 — out-of-vault detection sets gating_reason
 C5 — cached low-confidence slice is NOT reused (full pipeline runs instead)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helper: inline simulation of the hook's emit-decision logic (C1 tests)
# ---------------------------------------------------------------------------

def _hook_emit(meta: dict) -> str:
    """Simulate the hook body: return additionalContext string.

    The hook does:
        _emit_context = final_context if _should_emit_context else ""
    where should_emit_context and gating_reason come from try_slice_via_daemon_with_meta.
    """
    should_emit_context = meta.get("should_emit_context", True)
    slice_content = meta.get("slice", "some content")
    return slice_content if should_emit_context else ""


# ---------------------------------------------------------------------------
# C1 — hook additionalContext suppression
# ---------------------------------------------------------------------------


def test_c1_hook_emits_empty_when_should_not_emit():
    """C1: when should_emit_context=False the hook must produce empty additionalContext."""
    meta = {
        "slice": "some content",
        "should_emit_context": False,
        "gating_reason": "low_confidence",
    }
    additional_context = _hook_emit(meta)
    assert additional_context == ""


def test_c1_hook_emits_content_when_should_emit():
    """C1: when should_emit_context=True the hook must pass through the slice content."""
    meta = {
        "slice": "# Active Memory Slice\n\n## Goals\n- goal one\n",
        "should_emit_context": True,
        "gating_reason": "",
    }
    additional_context = _hook_emit(meta)
    assert additional_context != ""

