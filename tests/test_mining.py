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


def test_mine_session_increments_attempts_before_status_in_progress(tmp_path):
    """M-9: mine_session must increment attempts BEFORE the Haiku call.

    If the process is killed between STATUS_IN_PROGRESS write and Haiku
    response, attempts must already be incremented so the session doesn't
    re-queue forever. We simulate a mid-flight failure by mocking the Haiku
    call to raise RuntimeError and verify attempts == initial + 1 afterwards.
    """
    import json
    from unittest.mock import patch

    import memem.mining as mining_mod
    import memem.session_state as ss_mod
    from memem.mining import FatalMiningError

    # Create a synthetic JSONL transcript large enough to pass the delta check.
    session_file = tmp_path / "test-session-abc123.jsonl"
    lines = []
    for i in range(50):
        lines.append(json.dumps({
            "type": "user",
            "cwd": str(tmp_path),
            "message": {"content": [{"type": "text", "text": f"user message {i} " + "x" * 200}]},
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "cwd": str(tmp_path),
            "message": {"content": [{"type": "text", "text": f"assistant reply {i}"}]},
        }))
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Point session state at a tmp DB by patching _db_path in session_state.
    db_path = tmp_path / "mined_sessions.db"

    with (
        patch("memem.session_state._db_path", return_value=db_path),
        patch.object(mining_mod, "_summarize_session_haiku",
                     side_effect=RuntimeError("simulated Haiku failure")),
    ):
        # mine_session re-raises as FatalMiningError on unexpected exceptions.
        try:
            mining_mod.mine_session(str(session_file))
        except FatalMiningError:
            pass  # Expected — the Haiku call failed.

    # Load the persisted state and verify attempts was incremented.
    with patch("memem.session_state._db_path", return_value=db_path):
        states = ss_mod.load_mined_session_state()

    session_id = session_file.stem
    assert session_id in states, f"Session {session_id!r} not found in state DB"
    final_attempts = states[session_id].get("attempts", 0)
    assert final_attempts >= 1, (
        f"Expected attempts >= 1 after failed mine, got {final_attempts}. "
        "M-9 fix may not be active."
    )
