"""Tests for mining pipeline (mocked Haiku)."""



def test_extract_json_string():
    from memem.mining import _extract_json_string
    assert _extract_json_string('[{"title": "test"}]') == '[{"title": "test"}]'
    assert _extract_json_string('prefix [{"title": "x"}] suffix') == '[{"title": "x"}]'
    assert _extract_json_string('no json here') is None


def test_is_agent_session():
    from memem.mining import _is_agent_session
    assert _is_agent_session(["User: # Planner Module\nYou are a planner"])
    assert _is_agent_session(["User: You are a helpful assistant"])
    assert not _is_agent_session(["User: fix the auth bug"])


def test_build_chunks_short_messages_makes_progress():
    """Many tiny messages where overlap could cover the whole chunk must
    still advance the window by at least one message per iteration."""
    from memem.mining import _build_chunks
    messages = ["a" * 5 for _ in range(50)]
    chunks = _build_chunks(messages, max_chars=100, overlap_chars=95)
    assert len(chunks) > 1
    total_covered = sum(len(c) for c in chunks)
    assert total_covered >= len(messages), "all messages must appear in at least one chunk"


def test_build_chunks_no_overlap_is_simple_split():
    from memem.mining import _build_chunks
    messages = ["msg" * 20 for _ in range(10)]
    chunks = _build_chunks(messages, max_chars=200, overlap_chars=0)
    flat = [m for c in chunks for m in c]
    assert flat == messages
