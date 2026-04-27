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


# --- Cross-project score penalty tests (m2) ---


def test_same_project_scores_higher_than_cross_project_in_scope_strict():
    """scope_strict=True: same-project candidate must outscore an identical cross-project one."""
    from memem.activation import score_candidate_for_role

    same_project = {
        "candidate_id": "same",
        "candidate_type": "memory",
        "memory_id": "same",
        "title": "Auth implementation notes",
        "summary": "Auth implementation details and decisions made.",
        "project": "memem",
        "importance": 3,
        "score": 0.7,
    }
    cross_project = {
        "candidate_id": "cross",
        "candidate_type": "memory",
        "memory_id": "cross",
        "title": "Auth implementation notes",
        "summary": "Auth implementation details and decisions made.",
        "project": "other-project",
        "importance": 3,
        "score": 0.7,
    }
    environment = {"scope_id": "memem", "scope_strict": True}
    query = "auth implementation"

    same_score = score_candidate_for_role(same_project, "decisions", query, environment)
    cross_score = score_candidate_for_role(cross_project, "decisions", query, environment)

    assert same_score > cross_score, (
        f"Expected same-project score ({same_score:.4f}) > cross-project score ({cross_score:.4f})"
    )


def test_cross_project_candidate_still_appears_in_output_scope_strict():
    """scope_strict=True: cross-project candidates must not be dropped, only penalized."""
    from memem.activation import judge_activation_heuristically
    from memem.active_slice import current_query_candidate

    bundle = {
        "current_goal_candidates": [current_query_candidate("Fix auth bug", "memem")],
        "memory_candidates": [
            {
                "candidate_id": "same-mem",
                "candidate_type": "memory",
                "memory_id": "same-mem",
                "title": "Must preserve login constraint",
                "summary": "The fix must preserve the login constraint.",
                "project": "memem",
                "importance": 4,
                "score": 0.75,
            },
            {
                "candidate_id": "cross-mem",
                "candidate_type": "memory",
                "memory_id": "cross-mem",
                "title": "Constraint from other project",
                "summary": "Cross-project constraint that should remain visible.",
                "project": "other-project",
                "importance": 4,
                "score": 0.75,
            },
        ],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    result = judge_activation_heuristically(
        "Fix auth bug",
        "memem",
        {"scope_id": "memem", "scope_strict": True},
        bundle,
    )

    all_entries = (
        result.get("goals", [])
        + result.get("constraints", [])
        + result.get("decisions", [])
        + result.get("preferences", [])
        + result.get("failure_patterns", [])
        + result.get("artifact_context", [])
        + result.get("background", [])
    )
    all_ids = {entry["candidate_id"] for entry in all_entries}
    assert "cross-mem" in all_ids, "Cross-project candidate was dropped; it should remain visible-but-secondary."


def test_no_penalty_without_scope_strict():
    """scope_strict=False (default): cross-project candidates get no additional penalty."""
    from memem.activation import score_candidate_for_role

    candidate = {
        "candidate_id": "cross",
        "candidate_type": "memory",
        "memory_id": "cross",
        "title": "Decision from other project",
        "summary": "We decided to adopt this pattern across projects.",
        "project": "other-project",
        "importance": 3,
        "score": 0.7,
    }
    env_strict = {"scope_id": "memem", "scope_strict": True}
    env_default = {"scope_id": "memem"}
    query = "adopt pattern"

    score_no_strict = score_candidate_for_role(candidate, "decisions", query, env_default)
    score_strict = score_candidate_for_role(candidate, "decisions", query, env_strict)

    assert score_no_strict > score_strict, (
        f"Without scope_strict, cross-project score ({score_no_strict:.4f}) should exceed "
        f"scope_strict score ({score_strict:.4f})"
    )


def test_scope_strict_survives_environment_normalization():
    """Regression guard for C1: scope_strict must propagate through
    normalize_runtime_environment to reach the activation scorer.

    The previous implementation whitelisted recognized keys and silently
    dropped scope_strict, making the entire feature dead in production.
    """
    from memem.environment_context import normalize_runtime_environment

    raw = {"scope_id": "memem", "scope_strict": True}
    normalized = normalize_runtime_environment(raw)
    assert normalized.get("scope_strict") is True, (
        f"scope_strict was dropped during environment normalization: {normalized}"
    )

    raw_default = {"scope_id": "memem"}
    normalized_default = normalize_runtime_environment(raw_default)
    assert "scope_strict" not in normalized_default, (
        "scope_strict should not appear when not provided"
    )
