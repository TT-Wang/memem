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
