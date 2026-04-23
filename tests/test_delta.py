"""Tests for non-mutating delta proposals."""


def test_open_tension_delta_requires_confirmation():
    from memem.delta import propose_deltas_from_slice

    deltas = propose_deltas_from_slice({
        "slice_id": "slice_1",
        "open_tensions": [{
            "description": "Proposal scope is still fuzzy",
            "severity": "medium",
            "linked_memory_ids": ["abc12345"],
        }],
    })

    assert deltas
    assert deltas[0]["delta_type"] == "add_open_tension_memory"
    assert deltas[0]["requires_user_confirmation"] is True
    assert deltas[0]["source_slice_id"] == "slice_1"


def test_resolved_tension_proposes_durable_memory():
    from memem.delta import propose_deltas_from_slice

    deltas = propose_deltas_from_slice({
        "slice_id": "slice_2",
        "scope_id": "memem",
        "resolved_tensions": [{
            "description": "The rollout now keeps the old CLI alias until the migration lands.",
            "linked_memory_ids": ["feedface12345678"],
        }],
    })

    durable = next(delta for delta in deltas if delta["delta_type"] == "save_new_memory")
    assert durable["scope_id"] == "memem"
    assert durable["proposed_title"].startswith("Resolved tension")
    assert durable["requires_user_confirmation"] is False


def test_related_link_delta_deduplicates_targets_and_is_auto_safe_candidate():
    from memem.delta import propose_deltas_from_slice

    deltas = propose_deltas_from_slice({
        "slice_id": "slice_3",
        "scope_id": "memem",
        "constraints": [{"memory_id": "feedface12345678"}],
        "decisions": [{"memory_id": "feedface12345678"}],
        "failure_patterns": [{"memory_id": "deadbeef87654321"}],
    })

    related = next(delta for delta in deltas if delta["delta_type"] == "add_related_link")
    assert related["target_memory_ids"] == ["feedface12345678", "deadbeef87654321"]
    assert related["requires_user_confirmation"] is False
    assert related["scope_id"] == "memem"
