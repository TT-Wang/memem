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


def test_detect_project_from_cwd():
    """cwd→project helper: trailing segment, with home/root/empty → general."""
    import os

    from memem.mining import _detect_project_from_cwd
    assert _detect_project_from_cwd("/home/user/cortex-plugin") == "cortex-plugin"
    assert _detect_project_from_cwd("/home/user/substrate/") == "substrate"
    assert _detect_project_from_cwd("/opt/custom/my-project") == "my-project"
    assert _detect_project_from_cwd("") == "general"
    assert _detect_project_from_cwd("/") == "general"
    assert _detect_project_from_cwd(os.path.expanduser("~")) == "general"


def test_detect_session_cwd(tmp_path):
    """Reads cwd from the first usable record in a Claude Code session JSONL."""
    import json

    from memem.mining import _detect_session_cwd
    session = tmp_path / "sess.jsonl"
    session.write_text("\n".join([
        "",  # blank line first — should skip
        "not valid json",  # bad record — should skip
        json.dumps({"type": "user", "cwd": "/home/foo/bar", "message": {"content": "hi"}}),
        json.dumps({"type": "assistant", "cwd": "/home/foo/bar", "message": {"content": "ok"}}),
    ]))
    assert _detect_session_cwd(str(session)) == "/home/foo/bar"
    # Missing file returns empty string, not an exception
    assert _detect_session_cwd(str(tmp_path / "does-not-exist.jsonl")) == ""


def test_repair_json_bracket_in_string():
    """_repair_json must skip over string literals so a title like
    ``"see [note"`` doesn't fool the bracket counter into appending a
    spurious closer that produces invalid JSON."""
    import json

    from memem.mining import _repair_json

    balanced = '[{"title": "see [note", "content": "ok"}]'
    assert _repair_json(balanced) == balanced
    assert json.loads(_repair_json(balanced)) == [{"title": "see [note", "content": "ok"}]

    truncated_outer = '[{"title": "see [note", "content": "ok"}'
    assert json.loads(_repair_json(truncated_outer)) == [{"title": "see [note", "content": "ok"}]

    # Mid-string truncation: also closes the string
    truncated_mid = '[{"title": "truncated pr'
    parsed = json.loads(_repair_json(truncated_mid))
    assert parsed == [{"title": "truncated pr"}]


def test_normalize_scope_id_alias():
    """Aliases normalize to canonical names so consolidation doesn't skip
    a project tagged under a pre-rename alias (e.g. 'cortex' → 'cortex-plugin')."""
    from memem.models import _normalize_scope_id
    assert _normalize_scope_id("cortex") == "cortex-plugin"
    assert _normalize_scope_id("default") == "general"
    assert _normalize_scope_id("") == "general"
    assert _normalize_scope_id("substrate") == "substrate"
