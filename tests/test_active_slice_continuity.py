"""Tests for continuity-oriented environment and slice helpers."""

from __future__ import annotations


def test_task_mode_presets_normalize_to_stable_continuity_inputs():
    from memem.environment_context import normalize_runtime_environment

    env = normalize_runtime_environment({
        "task_mode": "session-start",
        "continuity": "full",
        "continuity_limit": "99",
        "continuity_summary_limit": "0",
        "continuity_artifact_limit": "5",
    })

    assert env["task_mode"] == "session_start"
    assert env["task_mode_preset"] == "session_start"
    assert env["continuity_mode"] == "full"
    assert env["continuity_focus"] == ["goals", "constraints", "artifacts"]
    assert env["continuity_slice_limit"] == 12
    assert env["continuity_summary_limit"] == 1
    assert env["continuity_artifact_limit"] == 5
    assert env["continuity_include_resolved"] is False


def test_carry_forward_summary_prioritizes_retained_constraints_tensions_and_artifacts():
    from memem.slice_history import annotate_slice_continuity

    previous = {
        "slice_id": "slice-1",
        "constraints": [{"memory_id": "mem-auth", "title": "Keep auth safe", "summary": "Keep auth safe"}],
        "open_tensions": [{"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"}],
        "artifacts": [{"artifact_id": "artifact_a", "title": "auth.py", "path": "/repo/auth.py", "summary": "Auth flow"}],
    }
    current = {
        "slice_id": "slice-2",
        "constraints": [{"memory_id": "mem-auth", "title": "Keep auth safe", "summary": "Keep auth safe"}],
        "open_tensions": [{"tension_id": "tension-owner", "description": "Owner unclear", "severity": "medium"}],
        "artifacts": [{"artifact_id": "artifact_a", "title": "auth.py", "path": "/repo/auth.py", "summary": "Auth flow updated"}],
        "resolved_tensions": [],
        "goals": [],
        "task_mode": "coding",
        "confidence": 0.7,
        "activation_mode": "heuristic",
    }

    annotated = annotate_slice_continuity(current, previous, carry_forward_limit=4)

    assert annotated["carry_forward_summary"] == [
        "Constraint: Keep auth safe",
        "Open tension: Owner unclear",
        "Artifact in progress: /repo/auth.py",
    ]
    assert annotated["artifact_progression"]["stage"] == "revising"
