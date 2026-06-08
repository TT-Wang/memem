"""Tests for memem.mine_delta incremental extractor."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    """Write a list of turn dicts to a JSONL file."""
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _append_jsonl(path: Path, turns: list[dict]) -> None:
    """Append turns to an existing JSONL file."""
    with path.open("a") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 5, substantive: bool = True) -> list[dict]:
    """Generate synthetic turns with enough text to pass the trivial-delta check."""
    if substantive:
        turns = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            turns.append({
                "role": role,
                "text": f"This is turn {i} with substantive content about the codebase architecture "
                        f"and important design decisions that should be remembered. "
                        f"The function uses a callback pattern and returns a list of dicts. " * 2,
            })
        return turns
    else:
        return [{"role": "user", "text": "ok"}]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mine_env(tmp_path, monkeypatch):
    """Set up isolated MEMEM_DIR and MEMEM_OBSIDIAN_VAULT for each test."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    (vault_dir / "memem" / "memories").mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    # Reload models so path constants pick up the new env vars
    import memem.models as _models
    importlib.reload(_models)
    import memem.obsidian_store as _store
    importlib.reload(_store)

    yield {
        "state_dir": state_dir,
        "vault_dir": vault_dir,
        "tmp_path": tmp_path,
    }

    # Reload again to avoid polluting other tests
    importlib.reload(_models)
    importlib.reload(_store)


# ---------------------------------------------------------------------------
# Canned Haiku fixture
# ---------------------------------------------------------------------------


CANNED_MEMORIES = [
    {
        "title": "Callback pattern",
        "project": "cortex-plugin",
        "content": "The function uses a callback pattern and returns a list of dicts.",
        "importance": 3,
    }
]


@pytest.fixture
def mock_haiku(monkeypatch):
    """Monkeypatch extract_from_text to return canned memories."""
    import memem.mine_delta as md

    def _fake_haiku(text: str, context_hint: str = "") -> list[dict]:
        return list(CANNED_MEMORIES)

    monkeypatch.setattr(md, "extract_from_text", _fake_haiku)
    return _fake_haiku


@pytest.fixture
def mock_haiku_empty(monkeypatch):
    """Monkeypatch extract_from_text to return empty list."""
    import memem.mine_delta as md

    def _fake_haiku_empty(text: str, context_hint: str = "") -> list[dict]:
        return []

    monkeypatch.setattr(md, "extract_from_text", _fake_haiku_empty)
    return _fake_haiku_empty


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOffsetAdvance:
    """test_offset_advance: second call sees only new turns."""

    def test_offset_advance(self, mine_env, mock_haiku):
        import memem.mine_delta as md

        transcript = mine_env["tmp_path"] / "session.jsonl"
        session_id = "test-offset-sess"

        # Write first batch of turns
        first_batch = _make_turns(4)
        _write_jsonl(transcript, first_batch)

        # First run
        md.run(session_id=session_id, transcript_path=str(transcript))
        offset_after_first = md._read_offset(session_id)
        assert offset_after_first > 0, "Offset should advance after first run"

        # Append second batch
        second_batch = _make_turns(4)
        _append_jsonl(transcript, second_batch)

        # Track how many Haiku calls happen
        call_count = [0]
        original = md.extract_from_text

        def counting_haiku(text, context_hint=""):
            call_count[0] += 1
            return original(text, context_hint)

        import memem.mine_delta as md2
        md2.extract_from_text = counting_haiku

        try:
            md.run(session_id=session_id, transcript_path=str(transcript))
        finally:
            md2.extract_from_text = original

        offset_after_second = md._read_offset(session_id)
        assert offset_after_second > offset_after_first, "Offset should advance again"


class TestFlockContention:
    """test_flock_contention: concurrent second invocation exits 0 without Haiku call."""

    def test_flock_contention(self, mine_env, monkeypatch):
        import fcntl

        import memem.mine_delta as md

        session_id = "test-flock-sess"

        # Pre-acquire the lock
        lock_path = md._lock_path(session_id)
        lock_fh = lock_path.open("w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        haiku_called = [False]

        def _fake_haiku(text, context_hint=""):
            haiku_called[0] = True
            return []

        monkeypatch.setattr(md, "extract_from_text", _fake_haiku)

        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(4))

        try:
            md.run(session_id=session_id, transcript_path=str(transcript))
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()

        assert not haiku_called[0], "Haiku should NOT be called when lock is held"


class TestEmptyDeltaSkip:
    """test_empty_delta_skip: delta < 100 chars → no Haiku call."""

    def test_empty_delta_skip(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-short-sess"

        haiku_called = [False]

        def _fake_haiku(text, context_hint=""):
            haiku_called[0] = True
            return []

        monkeypatch.setattr(md, "extract_from_text", _fake_haiku)

        transcript = mine_env["tmp_path"] / "session.jsonl"
        # 20 chars total
        _write_jsonl(transcript, [{"role": "user", "text": "short text here."}])

        md.run(session_id=session_id, transcript_path=str(transcript))

        assert not haiku_called[0], "Haiku should NOT be called for short delta"


class TestAckOnlySkip:
    """test_ack_only_skip: delta 'ok\nyes' → no Haiku call."""

    def test_ack_only_skip(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-ack-sess"

        haiku_called = [False]

        def _fake_haiku(text, context_hint=""):
            haiku_called[0] = True
            return []

        monkeypatch.setattr(md, "extract_from_text", _fake_haiku)

        transcript = mine_env["tmp_path"] / "session.jsonl"
        # Two turns that are ack-only — build enough chars but ack pattern check
        # uses the stripped content of individual non-empty lines.
        # The ack check works on combined stripped content; "ok yes" matches.
        # But the char count may be >= 100 since we joined lines.
        # Let's use a turn whose text is just "ok" and another "yes":
        turns = [
            {"role": "user", "text": "ok"},
            {"role": "assistant", "text": "yes"},
        ]
        _write_jsonl(transcript, turns)

        md.run(session_id=session_id, transcript_path=str(transcript))

        assert not haiku_called[0], "Haiku should NOT be called for ack-only delta"


class TestEmptyStreakBackoff:
    """test_empty_streak_backoff: 3 consecutive empty results → next 5 skip Haiku."""

    def test_empty_streak_backoff(self, mine_env, mock_haiku_empty):
        import memem.mine_delta as md

        session_id = "test-streak-sess"
        transcript = mine_env["tmp_path"] / "session.jsonl"

        # We need to manually drive the streak to 3 by calling run() 3 times,
        # each time with a fresh substantive delta (to pass trivial-skip check).
        # We reload the transcript each time since offsets advance.

        def fresh_transcript():
            """Write a new transcript so offset starts at 0 each call."""
            _write_jsonl(transcript, _make_turns(4))
            md._write_offset(session_id, 0)

        # Three empty Haiku runs
        for _i in range(3):
            fresh_transcript()
            md.run(session_id=session_id, transcript_path=str(transcript))

        streak, skip_remaining = md._read_empty_streak(session_id)
        assert streak >= 3, f"Streak should be >= 3, got {streak}"
        assert skip_remaining == md._EMPTY_STREAK_SKIP, (
            f"skip_remaining should be {md._EMPTY_STREAK_SKIP}, got {skip_remaining}"
        )

        # Next 5 invocations should skip Haiku (backoff)
        haiku_called = [0]
        original_empty = md.extract_from_text

        def counting_haiku(text, context_hint=""):
            haiku_called[0] += 1
            return []

        md.extract_from_text = counting_haiku
        try:
            for _ in range(5):
                fresh_transcript()
                md.run(session_id=session_id, transcript_path=str(transcript))
        finally:
            md.extract_from_text = original_empty

        assert haiku_called[0] == 0, (
            f"Haiku should NOT be called during backoff period, was called {haiku_called[0]} times"
        )


class TestHappyPath:
    """test_happy_path: substantive delta → Haiku invoked, memory written."""

    def test_happy_path(self, mine_env, mock_haiku):
        import memem.mine_delta as md

        session_id = "test-happy-sess"
        transcript = mine_env["tmp_path"] / "session.jsonl"

        _write_jsonl(transcript, _make_turns(6))

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Verify offset was advanced
        offset = md._read_offset(session_id)
        assert offset > 0, "Offset should be non-zero after run"

        # Verify session was recorded
        mined = md._mined_sessions_path().read_text()
        assert session_id in mined, "Session should be recorded in .mined_sessions"

        # Verify memory was written to vault
        vault = mine_env["vault_dir"]
        memories_dir = vault / "memem" / "memories"
        md_files = list(memories_dir.glob("*.md"))
        assert len(md_files) >= 1, f"Expected at least 1 memory file, found {len(md_files)}"

        # Check memory file contains expected content
        content = md_files[0].read_text()
        assert "Callback pattern" in content or "callback" in content.lower(), (
            f"Memory file should contain expected title/content. Got:\n{content[:500]}"
        )


# ---------------------------------------------------------------------------
# A1 regression: real Claude Code nested-schema parsing
# ---------------------------------------------------------------------------


def test_extract_role_text_handles_nested_claude_code_schema(mine_env):
    """Real Claude Code JSONL uses {type, message:{role, content}} not {role, text}.

    Pre-A1-fix the extractor read top-level role/text and returned empty
    strings for every real-format turn → trivial-delta skip → silent
    production mining failure. This test pins the fix in place.
    """
    import memem.mine_delta as md
    # Nested-schema turn with string content
    nested_str = {
        "type": "user",
        "message": {"role": "user", "content": "What's the status of v2.1.0?"},
    }
    role, text = md._extract_role_text(nested_str)
    assert role == "user"
    assert "v2.1.0" in text

    # Nested-schema turn with list-of-blocks content (assistant w/ tool_use mixed in)
    nested_list = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check the version."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "grep version"}},
                {"type": "text", "text": "Looks like 2.1.0."},
            ],
        },
    }
    role, text = md._extract_role_text(nested_list)
    assert role == "assistant"
    assert "check the version" in text
    assert "Looks like 2.1.0" in text
    # tool_use blocks must NOT leak into text
    assert "grep version" not in text

    # Legacy flat schema still works (back-compat)
    legacy = {"role": "user", "text": "Legacy fixture text"}
    role, text = md._extract_role_text(legacy)
    assert role == "user"
    assert text == "Legacy fixture text"

    # Non-conversational event (e.g. permission-mode) returns empty
    other = {"type": "permission-mode", "permissionMode": "default"}
    role, text = md._extract_role_text(other)
    assert text == ""


def test_turns_to_text_nonempty_on_real_schema(mine_env):
    """End-to-end: a mini JSONL with real Claude Code shape must produce text.

    Pre-fix this returned 0 chars regardless of turn count.
    """
    import memem.mine_delta as md
    turns = [
        {"type": "user", "message": {"role": "user", "content": "Hello, can you help?"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Yes, I can help."}],
            },
        },
    ]
    text = md._turns_to_text(turns)
    assert len(text) > 20, f"Expected real text, got {len(text)} chars: {text!r}"
    assert "Hello" in text
    assert "Yes, I can help" in text
