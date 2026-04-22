"""Tests for Active Memory Slice boundary rules."""


def _candidate(cid, project="memem", status="active", score=0.5, summary="candidate text"):
    return {
        "candidate_id": cid,
        "candidate_type": "memory",
        "memory_id": cid,
        "project": project,
        "status": status,
        "score": score,
        "title": cid,
        "summary": summary,
    }


def test_pre_boundaries_suppress_deprecated_and_cross_scope():
    from memem.boundaries import apply_pre_boundaries

    result = apply_pre_boundaries([
        _candidate("ok", project="memem"),
        _candidate("old", project="memem", status="deprecated"),
        _candidate("other", project="lexie"),
    ], scope_id="memem")

    ids = {c["candidate_id"] for c in result["candidates"]}
    reasons = {e["reason"] for e in result["excluded_candidates"]}
    assert ids == {"ok"}
    assert "deprecated" in reasons
    assert "cross_scope" in reasons


def test_post_boundaries_enforce_role_budget():
    from memem.boundaries import apply_post_boundaries

    candidates = [_candidate(f"c{i}", score=1 - i * 0.01) for i in range(10)]
    activation = {"constraints": [{"candidate_id": c["candidate_id"], "score": c["score"]} for c in candidates]}
    result = apply_post_boundaries(activation, candidates, "memem")

    assert len(result["constraints"]) == 6
    assert any(e["reason"] == "role_budget" for e in result["ignored"])


def test_post_boundaries_accept_artifact_id():
    from memem.boundaries import apply_post_boundaries

    candidate = {
        "candidate_id": "artifact_candidate",
        "artifact_id": "artifact_123",
        "candidate_type": "playbook",
        "project": "memem",
        "status": "active",
        "score": 0.7,
    }
    result = apply_post_boundaries(
        {"artifact_context": [{"artifact_id": "artifact_123", "score": 0.7}]},
        [candidate],
        "memem",
    )

    assert result["artifact_context"]
