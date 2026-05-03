"""Tests for 3-signal usage attribution (m2)."""
from __future__ import annotations

import json
from unittest.mock import patch

# ---------------------------------------------------------------------------
# embedding_similarity
# ---------------------------------------------------------------------------

def test_embedding_similarity_returns_float_0_to_1(monkeypatch):
    """Basic call returns a value in [0, 1]."""
    import numpy as np

    fake_vec = np.ones(4, dtype="float32").tolist()

    with patch("memem.embedding_index._embed_text", return_value=fake_vec):
        from memem.attribution import embedding_similarity
        result = embedding_similarity("some memory", "some response")
    assert isinstance(result, float)
    assert 0.0 <= result <= 1.0


def test_embedding_similarity_handles_empty_strings():
    """Returns 0.0 for empty inputs without calling the model."""
    from memem.attribution import embedding_similarity

    assert embedding_similarity("", "response text") == 0.0
    assert embedding_similarity("memory text", "") == 0.0
    assert embedding_similarity("", "") == 0.0


# ---------------------------------------------------------------------------
# citation_match
# ---------------------------------------------------------------------------

def test_citation_match_detects_8char_id():
    """Response containing the 8-char id prefix → True."""
    from memem.attribution import citation_match

    # memory_id starts with "abc12345", response contains that 8-char prefix
    assert citation_match("abc12345xyz", "My Memory Title", "see abc12345 for details") is True


def test_citation_match_detects_title_phrase():
    """Response containing first 3+ words of title → True."""
    from memem.attribution import citation_match

    title = "Use pytest fixtures for setup"
    # Response includes the exact first 5 words (lowercased) of the title
    response = "As described in use pytest fixtures for setup, testing is easier."
    assert citation_match("ffffffff000000001", title, response) is True


def test_citation_match_returns_false_when_unrelated():
    """No overlap between memory and response → False."""
    from memem.attribution import citation_match

    assert citation_match("deadbeef12345", "Pytest Fixture Setup Guide", "I love bananas and coconuts") is False


# ---------------------------------------------------------------------------
# should_run_judge
# ---------------------------------------------------------------------------

def test_should_run_judge_respects_sample_rate_zero():
    """sample_rate=0 always returns False."""
    from memem.attribution import should_run_judge

    for _ in range(20):
        assert should_run_judge(sample_rate=0.0) is False


def test_should_run_judge_respects_sample_rate_one():
    """sample_rate=1.0 always returns True."""
    from memem.attribution import should_run_judge

    for _ in range(20):
        assert should_run_judge(sample_rate=1.0) is True


# ---------------------------------------------------------------------------
# aggregate_signals
# ---------------------------------------------------------------------------

def test_aggregate_signals_judge_dominates_when_present():
    """judge=1.0, others=0 → result >= 0.6."""
    from memem.attribution import aggregate_signals

    result = aggregate_signals(emb_sim=0.0, citation=False, judge=1.0)
    assert result >= 0.6


def test_aggregate_signals_citation_boosts():
    """citation=True, others=0 → result >= 0.3."""
    from memem.attribution import aggregate_signals

    result = aggregate_signals(emb_sim=0.0, citation=True, judge=None)
    assert result >= 0.3


def test_aggregate_signals_returns_zero_when_no_evidence():
    """All signals weak → result < 0.1."""
    from memem.attribution import aggregate_signals

    result = aggregate_signals(emb_sim=0.0, citation=False, judge=None)
    assert result < 0.1


# ---------------------------------------------------------------------------
# log_slice_attribution
# ---------------------------------------------------------------------------

def test_log_slice_attribution_writes_event(tmp_cortex_dir):
    """Call log_slice_attribution; verify event appears in events log."""
    from memem.models import EVENT_LOG
    from memem.telemetry import log_slice_attribution

    log_slice_attribution(
        slice_id="slice-abc",
        memory_id="mem-xyz",
        embedding_sim=0.75,
        citation_match=True,
        judge_score=None,
        aggregate=0.675,
    )

    assert EVENT_LOG.exists()
    lines = EVENT_LOG.read_text().strip().splitlines()
    assert len(lines) >= 1
    event = json.loads(lines[-1])
    assert event["op"] == "slice_attribution"
    assert event["memory_id"] == "mem-xyz"
    assert event["slice_id"] == "slice-abc"
    assert event["citation_match"] is True
    assert event["judge_score"] is None
    assert abs(event["embedding_sim"] - 0.75) < 0.001


# ---------------------------------------------------------------------------
# record_slice_attribution
# ---------------------------------------------------------------------------

def test_record_slice_attribution_iterates_all_memory_items(tmp_cortex_dir):
    """Slice with 3 items; mock log_slice_attribution; verify called 3 times."""
    from memem.active_slice_engine import record_slice_attribution

    slice_data = {
        "slice_id": "test-slice-001",
        "query": "how do I test things",
        "items": [
            {"memory_id": "mem00001", "title": "Testing basics", "content": "Use pytest"},
            {"memory_id": "mem00002", "title": "Fixtures guide", "content": "Use conftest"},
            {"memory_id": "mem00003", "title": "Mocking tips", "content": "Use unittest.mock"},
        ],
    }

    with (
        patch("memem.telemetry.log_slice_attribution") as mock_log,
        # Ensure judge never fires (sample_rate=0 via env already, but be explicit)
        patch("memem.attribution.should_run_judge", return_value=False),
    ):
        record_slice_attribution(slice_data, "response about testing with pytest")

    assert mock_log.call_count == 3
    called_ids = {call.args[1] for call in mock_log.call_args_list}
    assert called_ids == {"mem00001", "mem00002", "mem00003"}
