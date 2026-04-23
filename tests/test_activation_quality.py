"""Quality-focused tests for Active Memory Slice activation."""

from __future__ import annotations


def test_constraint_candidate_ranked_above_generic_background():
    from memem.activation import judge_activation_heuristically
    from memem.active_slice import current_query_candidate

    bundle = {
        "current_goal_candidates": [current_query_candidate("Fix auth bug", "memem")],
        "memory_candidates": [
            {
                "candidate_id": "constraint",
                "candidate_type": "memory",
                "memory_id": "constraint",
                "title": "Must preserve login constraint",
                "summary": "The fix must preserve the login constraint path.",
                "project": "memem",
                "importance": 5,
                "score": 0.75,
            },
            {
                "candidate_id": "background",
                "candidate_type": "memory",
                "memory_id": "background",
                "title": "General auth notes",
                "summary": "Historical notes about auth flows.",
                "project": "memem",
                "importance": 2,
                "score": 0.8,
            },
        ],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    result = judge_activation_heuristically("Fix auth bug", "memem", {"scope_id": "memem"}, bundle)

    assert result["constraints"]
    assert result["constraints"][0]["candidate_id"] == "constraint"


def test_failure_pattern_candidate_detected():
    from memem.activation import judge_activation_heuristically

    bundle = {
        "current_goal_candidates": [],
        "memory_candidates": [{
            "candidate_id": "failure",
            "candidate_type": "memory",
            "memory_id": "failure",
            "title": "Regression issue",
            "summary": "Avoid the prior auth regression during rollout.",
            "project": "memem",
            "importance": 5,
            "score": 0.84,
        }],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    result = judge_activation_heuristically("Continue auth rollout", "memem", {"scope_id": "memem"}, bundle)

    assert result["failure_patterns"]
    assert result["failure_patterns"][0]["candidate_id"] == "failure"


def test_open_tension_extracted_from_unresolved_language():
    from memem.activation import judge_activation_heuristically

    bundle = {
        "current_goal_candidates": [],
        "memory_candidates": [{
            "candidate_id": "open-question",
            "candidate_type": "memory",
            "memory_id": "open-question",
            "title": "Open question",
            "summary": "The ownership tradeoff is still unresolved.",
            "project": "memem",
            "importance": 4,
            "score": 0.72,
        }],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    result = judge_activation_heuristically(
        "The rollout ownership is still unresolved",
        "memem",
        {"scope_id": "memem"},
        bundle,
    )

    assert result["open_tensions"]
    assert any("unresolved" in tension["description"].lower() or "tradeoff" in tension["description"].lower() for tension in result["open_tensions"])


def test_missing_artifact_support_creates_tension():
    from memem.activation import judge_activation_heuristically
    from memem.active_slice import current_query_candidate

    bundle = {
        "current_goal_candidates": [current_query_candidate("Update the proposal draft file", "memem")],
        "memory_candidates": [],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    result = judge_activation_heuristically(
        "Update the proposal draft file with the latest constraints",
        "memem",
        {"scope_id": "memem"},
        bundle,
    )

    assert any("artifact context" in tension["description"].lower() for tension in result["open_tensions"])


def test_role_specific_scoring_changes_order():
    from memem.activation import score_candidate_for_role

    decision_candidate = {
        "candidate_id": "decision",
        "candidate_type": "memory",
        "memory_id": "decision",
        "title": "Decision: keep slice-first runtime",
        "summary": "We decided to keep the slice-first runtime path.",
        "project": "memem",
        "importance": 5,
        "score": 0.7,
    }
    failure_candidate = {
        "candidate_id": "failure",
        "candidate_type": "memory",
        "memory_id": "failure",
        "title": "Failure pattern",
        "summary": "Avoid the previous regression in fallback handling.",
        "project": "memem",
        "importance": 5,
        "score": 0.7,
    }
    environment = {"scope_id": "memem"}

    decision_score = score_candidate_for_role(decision_candidate, "decisions", "Continue runtime work", environment)
    decision_as_failure = score_candidate_for_role(decision_candidate, "failure_patterns", "Continue runtime work", environment)
    failure_score = score_candidate_for_role(failure_candidate, "failure_patterns", "Continue runtime work", environment)
    failure_as_decision = score_candidate_for_role(failure_candidate, "decisions", "Continue runtime work", environment)

    assert decision_score > decision_as_failure
    assert failure_score > failure_as_decision
