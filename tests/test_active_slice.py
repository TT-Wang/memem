"""Tests for Active Memory Slice schemas and rendering."""


def test_current_query_goal_without_memories():
    from memem.active_slice import build_active_memory_slice, render_slice_as_prompt_context

    candidate_bundle = {"current_goal_candidates": [], "memory_candidates": []}
    slice_obj = build_active_memory_slice(
        "Continue the memory proposal",
        "memem",
        {},
        candidate_bundle,
        {"goals": [], "activation_mode": "heuristic"},
    )

    assert slice_obj["goals"][0]["source_type"] == "current_query"
    assert "Continue the memory proposal" in slice_obj["goals"][0]["summary"]

    rendered = render_slice_as_prompt_context(slice_obj)
    assert rendered == ""
    assert slice_obj["should_emit_context"] is False


def test_memory_candidate_normalization():
    from memem.active_slice import normalize_memory_candidate

    candidate = normalize_memory_candidate({
        "id": "abcdef123456",
        "title": "Use typed graph",
        "essence": "Memem uses typed graph edges for related memories.",
        "project": "memem",
        "importance": 4,
        "layer": 1,
    }, score=0.9)

    assert candidate["candidate_id"] == "memory:abcdef12"
    assert candidate["candidate_type"] == "memory"
    assert candidate["score"] == 0.9
    assert candidate["summary"].startswith("Memem uses typed graph")


def test_build_slice_resolves_artifact_id_activation():
    from memem.active_slice import build_active_memory_slice, normalize_artifact_candidate

    artifact = normalize_artifact_candidate(
        "playbook",
        "memem playbook",
        "Use active slices for runtime working state.",
        path="/tmp/playbook.md",
        project="memem",
    )
    slice_obj = build_active_memory_slice(
        "Continue memem",
        "memem",
        {},
        {"current_goal_candidates": [], "artifact_candidates": [artifact]},
        {"artifact_context": [{"artifact_id": artifact["artifact_id"], "why": "playbook", "score": 0.8}]},
    )

    assert slice_obj["artifacts"]
    assert slice_obj["artifacts"][0]["artifact_id"] == artifact["artifact_id"]
