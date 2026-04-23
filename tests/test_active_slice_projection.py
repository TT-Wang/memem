"""Projection tests for rendered Active Memory Slices."""

from __future__ import annotations


def test_render_slice_order_goals_constraints_tensions():
    from memem.active_slice import render_slice_as_prompt_context

    rendered = render_slice_as_prompt_context({
        "scope_id": "memem",
        "query": "Fix the auth flow",
        "activation_mode": "heuristic",
        "confidence": 0.71,
        "should_emit_context": True,
        "goals": [{"title": "Fix auth flow", "summary": "Restore the login path."}],
        "constraints": [{"title": "Constraint", "summary": "Do not break session refresh."}],
        "decisions": [],
        "failure_patterns": [],
        "open_tensions": [{"severity": "high", "description": "Refresh token ownership is unresolved.", "why_open": "tradeoff remains open"}],
        "artifacts": [{"title": "auth.py", "summary": "Current login implementation."}],
        "preferences": [],
        "active_background": [],
        "candidate_deltas": [],
        "warnings": [],
    })

    goals_index = rendered.index("## Goals")
    constraints_index = rendered.index("## Constraints")
    tensions_index = rendered.index("## Open Tensions")
    artifacts_index = rendered.index("## Artifacts")

    assert goals_index < constraints_index < tensions_index < artifacts_index


def test_render_slice_omits_empty_warning_section():
    from memem.active_slice import render_slice_as_prompt_context

    rendered = render_slice_as_prompt_context({
        "scope_id": "memem",
        "query": "Continue the proposal",
        "activation_mode": "heuristic",
        "confidence": 0.66,
        "should_emit_context": True,
        "goals": [{"title": "Continue proposal", "summary": "Carry forward the current draft."}],
        "constraints": [],
        "decisions": [],
        "failure_patterns": [],
        "open_tensions": [],
        "artifacts": [],
        "preferences": [],
        "active_background": [],
        "candidate_deltas": [],
        "warnings": [],
    })

    assert "## Warnings" not in rendered
    assert "## Candidate Deltas" not in rendered


def test_render_slice_respects_should_emit_context_false():
    from memem.active_slice import render_slice_as_prompt_context

    rendered = render_slice_as_prompt_context({
        "scope_id": "memem",
        "query": "Empty query",
        "activation_mode": "heuristic",
        "confidence": 0.2,
        "should_emit_context": False,
        "goals": [{"title": "Only current query", "summary": "No recalled support."}],
    })

    assert rendered == ""


def test_render_slice_surfaces_continuity_and_writeback_when_present():
    from memem.active_slice import render_slice_as_prompt_context

    rendered = render_slice_as_prompt_context({
        "scope_id": "memem",
        "query": "Continue proposal revision",
        "activation_mode": "heuristic",
        "confidence": 0.81,
        "should_emit_context": True,
        "task_mode": "proposal",
        "previous_slice_id": "slice_prev",
        "goals": [{"title": "Continue proposal", "summary": "Carry forward the draft revision."}],
        "constraints": [],
        "decisions": [],
        "failure_patterns": [],
        "open_tensions": [{"severity": "medium", "description": "Review ownership is still open.", "why_open": "waiting on stakeholder"}],
        "resolved_tensions": [{"severity": "low", "description": "Naming issue resolved.", "why_open": "closed in prior draft"}],
        "carry_forward_summary": ["Keep the rollout constraint visible.", "Preserve the approval path."],
        "artifacts": [{"title": "proposal.md", "summary": "Current proposal draft."}],
        "preferences": [],
        "active_background": [],
        "candidate_deltas": [],
        "delta_results": [{"delta_type": "add_related_link", "status": "dry_run", "commit_policy": "auto_safe", "result_message": "validated only"}],
        "writeback_summary": {"status": "dry_run", "proposed_count": 1, "auto_committed_count": 0, "manual_review_count": 1, "blocked_count": 0, "rejected_count": 0, "dry_run": True},
        "warnings": [],
    })

    assert "- task mode: proposal" in rendered
    assert "- previous slice: slice_prev" in rendered
    assert "## Resolved Tensions" in rendered
    assert "## Carry Forward" in rendered
    assert "## Writeback" in rendered
