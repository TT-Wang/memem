"""Tests for Active Memory Slice activation judgement."""


def test_heuristic_activation_classifies_roles():
    from memem.activation import judge_activation_heuristically
    from memem.active_slice import current_query_candidate

    bundle = {
        "current_goal_candidates": [current_query_candidate("Fix auth bug", "memem")],
        "memory_candidates": [
            {
                "candidate_id": "memory:a",
                "candidate_type": "memory",
                "memory_id": "a",
                "title": "Must use pytest",
                "summary": "Tests must use pytest fixtures",
                "score": 0.8,
            },
            {
                "candidate_id": "memory:b",
                "candidate_type": "memory",
                "memory_id": "b",
                "title": "Regression issue",
                "summary": "Avoid prior regression in auth flow",
                "score": 0.7,
            },
        ],
    }

    result = judge_activation_heuristically("Fix auth bug", "memem", {}, bundle)

    assert result["goals"]
    assert result["constraints"]
    assert result["failure_patterns"]
    assert result["activation_mode"] == "heuristic"


def test_llm_activation_falls_back_when_unavailable(monkeypatch):
    from memem import activation
    from memem.active_slice import current_query_candidate

    monkeypatch.setattr(activation, "assembly_available", lambda: False)
    result = activation.judge_activation_with_llm(
        "Continue proposal",
        "memem",
        {},
        {"current_goal_candidates": [current_query_candidate("Continue proposal", "memem")]},
    )

    assert result["activation_mode"] == "heuristic"
    assert result["warnings"]


def test_llm_activation_parses_valid_json(monkeypatch):
    import subprocess

    from memem import activation
    from memem.active_slice import current_query_candidate

    monkeypatch.setattr(activation, "assembly_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout='{"goals":[{"candidate_id":"current_query","why":"goal","score":0.9}]}', stderr=""
    ))

    result = activation.judge_activation_with_llm(
        "Continue proposal",
        "memem",
        {},
        {"current_goal_candidates": [current_query_candidate("Continue proposal", "memem")]},
    )

    assert result["activation_mode"] == "llm"
    assert result["goals"][0]["candidate_id"] == "current_query"
