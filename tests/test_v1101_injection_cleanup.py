"""Tests for v1.10.1 injection cleanup.

Covers:
1. Render-trim tests: sections removed from render_slice_markdown output
   (carry_forward_summary, candidate_deltas, writeback_summary, warnings)
   while the data schema still carries those fields.
2. Auto-mode trivial-ack gating: generate_active_memory_slice gates
   pure-acknowledgment queries in auto mode via the _AUTO_PURE_ACK_TOKENS set.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Minimal slice stub helpers
# ---------------------------------------------------------------------------


def _make_active_slice(**overrides) -> dict:
    """Build a minimal active-kind slice dict that render_slice_markdown accepts."""
    base: dict = {
        "slice_kind": "active",
        "scope_id": "test",
        "query": "test query",
        "activation_mode": "heuristic",
        "confidence": 0.75,
        "should_emit_context": True,
        "goals": [{"title": "Test goal", "summary": "A test goal.", "why_activated": ""}],
        "constraints": [],
        "decisions": [],
        "failure_patterns": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "artifacts": [],
        "preferences": [],
        "active_background": [],
        "candidate_deltas": [],
        "warnings": [],
        "carry_forward_summary": [],
        "writeback_summary": {
            "status": "not_run",
            "dry_run": True,
            "proposed_count": 0,
            "auto_committed_count": 0,
            "manual_review_count": 0,
            "blocked_count": 0,
            "rejected_count": 0,
        },
        "delta_results": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Render-trim tests (1-5)
# ---------------------------------------------------------------------------


def test_active_slice_render_no_carry_forward_section():
    """carry_forward_summary is present in slice dict but NOT rendered."""
    from memem.active_slice import render_slice_markdown

    slice_obj = _make_active_slice(carry_forward_summary=["foo", "bar"])
    output = render_slice_markdown(slice_obj)

    assert "## Carry Forward" not in output


def test_active_slice_render_no_candidate_deltas_section():
    """candidate_deltas are present in the slice dict but NOT rendered as a section."""
    from memem.active_slice import render_slice_markdown

    candidate_delta = {
        "delta_type": "add_tag",
        "reason": "Tag the memory with context.",
        "confidence": 0.8,
    }
    slice_obj = _make_active_slice(candidate_deltas=[candidate_delta])
    output = render_slice_markdown(slice_obj)

    assert "## Candidate Deltas" not in output


def test_active_slice_render_no_writeback_section():
    """writeback_summary is in the slice dict but NOT rendered as a section."""
    from memem.active_slice import render_slice_markdown

    slice_obj = _make_active_slice(
        writeback_summary={
            "status": "not_run",
            "dry_run": True,
            "proposed_count": 3,
            "auto_committed_count": 0,
            "manual_review_count": 0,
            "blocked_count": 0,
            "rejected_count": 0,
        }
    )
    output = render_slice_markdown(slice_obj)

    assert "## Writeback" not in output


def test_active_slice_render_no_warnings_section():
    """warnings are in the slice dict but NOT rendered as a section."""
    from memem.active_slice import render_slice_markdown

    slice_obj = _make_active_slice(warnings=["LLM activation disabled"])
    output = render_slice_markdown(slice_obj)

    assert "## Warnings" not in output


def test_slice_dict_still_has_carry_forward_field():
    """Data schema preserves carry_forward_summary even though it is not rendered."""
    from memem.active_slice import build_active_memory_slice

    candidate_bundle = {
        "current_goal_candidates": [],
        "memory_candidates": [],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }
    activation_result = {
        "goals": [],
        "activation_mode": "heuristic",
        "confidence": 0.6,
        "warnings": [],
    }
    slice_obj = build_active_memory_slice(
        query="check field presence",
        scope_id="test",
        environment={"session_id": "field-presence-test"},
        candidate_bundle=candidate_bundle,
        activation_result=activation_result,
    )

    # The field must exist in the dict (schema is intact)
    assert "carry_forward_summary" in slice_obj
    # Its default value is an empty list
    assert isinstance(slice_obj["carry_forward_summary"], list)


# ---------------------------------------------------------------------------
# Auto-mode trivial-ack gating tests (6-9)
# ---------------------------------------------------------------------------


def _call_generate_auto(query: str, monkeypatch, session_id: str) -> dict:
    """Call generate_active_memory_slice in auto mode with isolated session state."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", "auto")

    import memem.slice_history as sh

    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()

    from memem.active_slice_engine import generate_active_memory_slice

    return generate_active_memory_slice(
        query,
        scope_id="test",
        environment={"session_id": session_id},
        use_llm=False,
    )


def test_auto_mode_skips_pure_yes_query(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, 'yes' is gated with gating_reason='trivial_query'."""
    result = _call_generate_auto("yes", monkeypatch, session_id="auto-ack-test-1")

    assert result.get("gating_reason") == "trivial_query", (
        f"Expected 'trivial_query', got {result.get('gating_reason')!r}"
    )
    assert result.get("should_emit_context") is False


def test_auto_mode_skips_chinese_pure_ack(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, '好的' (Chinese ack) is gated with gating_reason='trivial_query'."""
    result = _call_generate_auto("好的", monkeypatch, session_id="auto-ack-test-2")

    assert result.get("gating_reason") == "trivial_query", (
        f"Expected 'trivial_query', got {result.get('gating_reason')!r}"
    )
    assert result.get("should_emit_context") is False


def test_auto_mode_does_not_skip_substantive_query(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, a substantive query is NOT gated by trivial-ack logic."""
    result = _call_generate_auto(
        "how do I fix this bug",
        monkeypatch,
        session_id="auto-ack-test-3",
    )

    assert result.get("gating_reason") != "trivial_query", (
        f"Substantive query should not be trivially gated; got {result.get('gating_reason')!r}"
    )


def test_auto_mode_does_not_skip_4_word_query(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, 'yes please do that' (4 tokens) is NOT gated (limit is <=3 tokens)."""
    result = _call_generate_auto(
        "yes please do that",
        monkeypatch,
        session_id="auto-ack-test-4",
    )

    assert result.get("gating_reason") != "trivial_query", (
        f"4-token query should not be trivially gated; got {result.get('gating_reason')!r}"
    )
