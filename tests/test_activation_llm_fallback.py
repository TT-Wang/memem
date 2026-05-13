"""Tests for judge_activation bulletproof fallback wrapper."""
import logging


def _make_bundle(query: str = "Fix auth bug", scope: str = "memem") -> dict:
    from memem.active_slice import current_query_candidate

    return {
        "current_goal_candidates": [current_query_candidate(query, scope)],
        "memory_candidates": [
            {
                "candidate_id": "memory:constraint-a",
                "candidate_type": "memory",
                "memory_id": "constraint-a",
                "title": "Must use pytest",
                "summary": "Tests must use pytest fixtures",
                "score": 0.8,
            },
            {
                "candidate_id": "memory:failure-b",
                "candidate_type": "memory",
                "memory_id": "failure-b",
                "title": "Regression risk",
                "summary": "Avoid prior regression in auth flow",
                "score": 0.7,
            },
        ],
    }


def test_judge_activation_falls_back_on_exception(monkeypatch, caplog):
    """Test 1: when judge_activation_with_llm raises, heuristic result is returned and WARNING is logged."""
    from memem import activation

    def _raise(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(activation, "judge_activation_with_llm", _raise)

    bundle = _make_bundle()
    with caplog.at_level(logging.WARNING, logger="memem-activation"):
        result = activation.judge_activation("Fix auth bug", "memem", {}, bundle, use_llm=True)

    # Should have returned a valid heuristic result
    assert result["activation_mode"] == "heuristic"
    assert result["goals"]
    # Should have logged a WARNING
    assert any("network down" in record.message for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_judge_activation_uses_llm_result_when_available(monkeypatch):
    """Test 2: when judge_activation_with_llm returns a valid result, it is used."""
    from memem import activation

    fake_result = {
        "goals": [{"candidate_id": "current_query", "memory_id": "", "artifact_id": "", "why": "goal", "score": 0.9, "centrality": 0.9, "role_confidence": 0.9, "drop_reason": ""}],
        "constraints": [],
        "background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifact_context": [],
        "open_tensions": [],
        "ignored": [],
        "activation_mode": "llm",
        "confidence": 0.95,
        "warnings": [],
    }

    monkeypatch.setattr(activation, "judge_activation_with_llm", lambda *a, **k: fake_result)

    bundle = _make_bundle()
    result = activation.judge_activation("Fix auth bug", "memem", {}, bundle, use_llm=True)

    assert result["activation_mode"] == "llm"
    assert result["confidence"] == 0.95
    assert result["goals"][0]["candidate_id"] == "current_query"


def test_judge_activation_skips_llm_when_use_llm_false(monkeypatch):
    """Test 3: use_llm=False always uses heuristic, never calls LLM."""
    from memem import activation

    llm_called = []

    def _should_not_be_called(*args, **kwargs):
        llm_called.append(True)
        raise AssertionError("LLM should not be called when use_llm=False")

    monkeypatch.setattr(activation, "judge_activation_with_llm", _should_not_be_called)

    bundle = _make_bundle()
    result = activation.judge_activation("Fix auth bug", "memem", {}, bundle, use_llm=False)

    assert not llm_called
    assert result["activation_mode"] == "heuristic"


def test_active_slice_engine_produces_valid_slice_when_llm_unavailable(monkeypatch):
    """Test 4 (regression): active_slice_engine still produces valid slices when LLM raises."""
    from memem import activation
    from memem.active_slice_engine import generate_active_memory_slice

    def _raise(*args, **kwargs):
        raise RuntimeError("LLM endpoint unreachable")

    monkeypatch.setattr(activation, "judge_activation_with_llm", _raise)

    # Patch out recall and other I/O so it doesn't hit the filesystem
    monkeypatch.setattr(
        "memem.active_slice_engine.generate_candidates",
        lambda query, scope_id, env, **kwargs: {
            "current_goal_candidates": [
                {
                    "candidate_id": f"current_query:{query}",
                    "candidate_type": "current_query",
                    "memory_id": "",
                    "artifact_id": "",
                    "title": query,
                    "summary": query,
                    "content": "",
                    "score": 1.0,
                    "project": scope_id,
                }
            ],
            "memory_candidates": [],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        },
    )
    # Patch history persistence to no-op
    monkeypatch.setattr("memem.active_slice_engine._persist_slice", lambda s: None)

    slice_obj = generate_active_memory_slice("Implement new feature", scope_id="memem", use_llm=True)

    # Slice must have the expected structural keys (ActiveMemorySlice renames some fields
    # from ActivationResult: background -> active_background, artifact_context -> artifacts)
    for key in ("goals", "constraints", "active_background", "decisions", "preferences", "failure_patterns", "artifacts", "open_tensions"):
        assert key in slice_obj, f"Missing key in slice: {key}"

    # Goals should be populated (the current_query candidate should land in goals)
    assert slice_obj["goals"]
