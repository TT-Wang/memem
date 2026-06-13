"""Tests for the canonical parse_jsonl_session in memem/transcripts.py."""
import importlib
import json
import os
from pathlib import Path

import pytest


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write a list of dicts as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _make_session_records() -> list[dict]:
    """A synthetic session with user, assistant, system, and tool_use messages."""
    return [
        # system record — should be kept (new canonical includes "system")
        {
            "type": "system",
            "message": {"content": "You are a helpful assistant."},
        },
        # user message with plain text
        {
            "type": "user",
            "message": {"content": "Please fix the auth bug."},
            "timestamp": "2026-05-08T10:00:00Z",
        },
        # assistant message with text + tool_use
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Sure, let me read the file."},
                    {"type": "tool_use", "name": "Read", "id": "t1", "input": {"file_path": "/src/auth.py"}},
                ]
            },
            "timestamp": "2026-05-08T10:00:05Z",
        },
        # another user message
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "Thanks. Also check the test."},
                ]
            },
            "timestamp": "2026-05-08T10:01:00Z",
        },
        # assistant with Bash tool_use and text
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Running the tests now."},
                    {"type": "tool_use", "name": "Bash", "id": "t2", "input": {"command": "pytest tests/"}},
                ]
            },
            "timestamp": "2026-05-08T10:01:10Z",
        },
        # unknown type — should be dropped
        {
            "type": "tool_result",
            "message": {"content": "Result data"},
        },
        # empty content — should be omitted
        {
            "type": "user",
            "message": {"content": ""},
        },
    ]


def test_parse_jsonl_session_round_trip(tmp_path):
    """Canonical parser returns all kept roles with non-empty text/tool_use."""
    from memem.transcripts import parse_jsonl_session

    session = tmp_path / "session.jsonl"
    _write_jsonl(session, _make_session_records())

    messages = parse_jsonl_session(str(session))

    # Should have: system, user, assistant (text+tool_use), user, assistant (text+tool_use)
    # The empty-content user record at the end is dropped.
    assert len(messages) == 5, f"Expected 5 messages, got {len(messages)}: {messages}"

    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


def test_parse_jsonl_session_role_filtering(tmp_path):
    """Roles other than user/assistant/system are dropped."""
    from memem.transcripts import parse_jsonl_session

    records = [
        {"type": "user", "message": {"content": "Hello"}},
        {"type": "tool_result", "message": {"content": "Some result"}},
        {"type": "summary", "message": {"content": "A summary"}},
        {"type": "assistant", "message": {"content": "Hi there"}},
    ]
    session = tmp_path / "filter_test.jsonl"
    _write_jsonl(session, records)

    messages = parse_jsonl_session(str(session))
    roles = [m["role"] for m in messages]

    assert "tool_result" not in roles
    assert "summary" not in roles
    assert "user" in roles
    assert "assistant" in roles


def test_parse_jsonl_session_tool_use_field(tmp_path):
    """tool_use field is populated for messages with tool_use blocks."""
    from memem.transcripts import parse_jsonl_session

    records = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me edit the file."},
                    {"type": "tool_use", "name": "Edit", "id": "e1",
                     "input": {"file_path": "/src/main.py"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": "Great."},
        },
    ]
    session = tmp_path / "tool_use_test.jsonl"
    _write_jsonl(session, records)

    messages = parse_jsonl_session(str(session))

    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["tool_use"] is not None
    assert "Edit" in assistant_msgs[0]["tool_use"]

    user_msgs = [m for m in messages if m["role"] == "user"]
    assert user_msgs[0]["tool_use"] is None  # no tool_use in plain user message


def test_parse_jsonl_session_start_offset(tmp_path):
    """start_offset causes the parser to skip bytes before that offset."""
    from memem.transcripts import parse_jsonl_session

    # Write two records; record the byte position after the first.
    record1 = {"type": "user", "message": {"content": "First message"}}
    record2 = {"type": "assistant", "message": {"content": "Second message"}}

    session = tmp_path / "offset_test.jsonl"
    line1 = json.dumps(record1) + "\n"
    line2 = json.dumps(record2) + "\n"
    session.write_bytes(line1.encode("utf-8") + line2.encode("utf-8"))

    offset_after_first = len(line1.encode("utf-8"))

    # Without offset: both records.
    all_msgs = parse_jsonl_session(str(session))
    assert len(all_msgs) == 2

    # With offset skipping the first record: only the second.
    partial_msgs = parse_jsonl_session(str(session), start_offset=offset_after_first)
    assert len(partial_msgs) == 1
    assert partial_msgs[0]["role"] == "assistant"
    assert partial_msgs[0]["text"] == "Second message"


def test_parse_jsonl_session_empty_file(tmp_path):
    """Empty file returns empty list without crashing."""
    from memem.transcripts import parse_jsonl_session

    session = tmp_path / "empty.jsonl"
    session.write_text("", encoding="utf-8")

    messages = parse_jsonl_session(str(session))
    assert messages == []


def test_parse_jsonl_session_empty_messages_not_included(tmp_path):
    """Records with empty content and no tool_use are omitted."""
    from memem.transcripts import parse_jsonl_session

    records = [
        {"type": "user", "message": {"content": ""}},
        {"type": "assistant", "message": {"content": []}},
        {"type": "user", "message": {"content": "Hello"}},
    ]
    session = tmp_path / "empty_msg.jsonl"
    _write_jsonl(session, records)

    messages = parse_jsonl_session(str(session))
    assert len(messages) == 1
    assert messages[0]["text"] == "Hello"


def test_parse_jsonl_session_invalid_json_lines_skipped(tmp_path):
    """Malformed JSON lines do not crash the parser."""
    from memem.transcripts import parse_jsonl_session

    session = tmp_path / "bad_json.jsonl"
    content = (
        '{"type": "user", "message": {"content": "Good line"}}\n'
        "not valid json\n"
        "\n"
        '{"type": "assistant", "message": {"content": "Also good"}}\n'
    )
    session.write_text(content, encoding="utf-8")

    messages = parse_jsonl_session(str(session))
    assert len(messages) == 2
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"]


def test_parse_jsonl_session_missing_file():
    """Missing file returns empty list without raising."""
    from memem.transcripts import parse_jsonl_session

    messages = parse_jsonl_session("/nonexistent/path/session.jsonl")
    assert messages == []


# ---------------------------------------------------------------------------
# FTS5 index tests
# ---------------------------------------------------------------------------

# Path to the production-shape fixture (real Claude Code JSONL format).
_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_REAL_SESSION_FIXTURE = _FIXTURE_DIR / "real_session_sample.jsonl"


@pytest.fixture
def fts_env(tmp_path, monkeypatch):
    """Isolate MEMEM_DIR (and SESSIONS_DIRS) to tmp_path for all FTS tests.

    SESSIONS_DIRS is redirected to an empty dir so the grep fallback in
    transcript_search() can never scan the real (potentially huge) session
    history. Without this, any test whose query misses the small FTS index
    falls through to grep over the live session dirs and effectively hangs.
    Tests that need specific sessions override SESSIONS_DIRS themselves.
    """
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    # Reload transcripts so _db_path() will resolve using our patched env
    import memem.transcripts as _tr
    importlib.reload(_tr)
    # Isolate the grep fallback to an empty dir by default.
    empty_sessions = tmp_path / "empty_sessions"
    empty_sessions.mkdir()
    import memem.session_state as _ss
    monkeypatch.setattr(_ss, "SESSIONS_DIRS", [empty_sessions])
    monkeypatch.setattr(_tr, "SESSIONS_DIRS", [empty_sessions])
    return state


def test_fts_roundtrip_real_session(fts_env, tmp_path):
    """FTS5 roundtrip using real production-shape fixture.

    1. index_session() indexes real_session_sample.jsonl
    2. transcript_fts.db exists with >= 1 row
    3. transcript_search for a term that exists returns non-empty result
    """
    import sqlite3
    import memem.transcripts as tr

    # Use the real fixture
    assert _REAL_SESSION_FIXTURE.exists(), f"Fixture missing: {_REAL_SESSION_FIXTURE}"

    tr.index_session(_REAL_SESSION_FIXTURE)

    db_path = tr._db_path()
    assert db_path.exists(), "transcript_fts.db was not created"

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    conn.close()
    assert count >= 1, f"Expected >= 1 row in transcripts table, got {count}"

    # Search for a term we know is in the fixture (from the first user message)
    result = tr.transcript_search("memory", limit=5)
    assert result != "No matching transcripts found", (
        f"Expected non-empty result for 'memory' query, got: {result!r}"
    )


def test_fts_fallback_when_db_absent(fts_env, monkeypatch):
    """transcript_search falls back to grep when FTS DB is absent.

    With DB absent AND no real session dirs (SESSIONS_DIRS pointing at empty),
    the function must return 'No matching transcripts found' without raising.
    """
    import memem.transcripts as tr
    import memem.session_state as ss

    # Point SESSIONS_DIRS at a tmp dir with no JSONL files
    empty_dir = fts_env / "empty_sessions"
    empty_dir.mkdir()
    monkeypatch.setattr(ss, "SESSIONS_DIRS", [empty_dir])
    # Also patch into the transcripts module namespace
    monkeypatch.setattr(tr, "SESSIONS_DIRS", [empty_dir])

    # Ensure the DB does NOT exist
    db_path = tr._db_path()
    assert not db_path.exists(), "DB should not exist for this test"

    result = tr.transcript_search("anything", limit=5)
    assert result == "No matching transcripts found"


def test_fts_operator_injection_no_crash(fts_env):
    """FTS5 operator-injection query must not crash transcript_search."""
    import memem.transcripts as tr

    # Index the real fixture first so DB exists
    tr.index_session(_REAL_SESSION_FIXTURE)

    # These contain FTS5 operators and unbalanced quotes — must not raise
    dangerous_queries = [
        'test AND "unbalanced',
        'OR NOT',
        '"open quote',
        'NEAR(foo bar)',
        'a * b',
        '"double" AND "quotes"',
    ]
    for q in dangerous_queries:
        try:
            result = tr.transcript_search(q, limit=3)
            # Result can be anything as long as no exception was raised
            assert isinstance(result, str), f"Expected str result for query {q!r}"
        except Exception as exc:
            pytest.fail(f"transcript_search raised for query {q!r}: {exc}")


def test_fts_output_shape_matches_grep_path(fts_env, tmp_path, monkeypatch):
    """FTS5 and grep paths produce identically-shaped markdown output.

    Both paths must produce output that contains:
    - '**Q:**' prefix for user text
    - '**A:**' prefix for assistant text
    - '*Session:' for filename annotation
    """
    import memem.transcripts as tr
    import memem.session_state as ss

    # Index the real fixture (FTS path)
    tr.index_session(_REAL_SESSION_FIXTURE)

    # Get FTS result
    fts_result = tr.transcript_search("memory", limit=3)

    if fts_result == "No matching transcripts found":
        pytest.skip("Fixture produced no FTS results for 'memory'")

    # Verify FTS result shape
    assert "**Q:**" in fts_result, f"FTS result missing **Q:** marker: {fts_result[:200]}"
    assert "**A:**" in fts_result, f"FTS result missing **A:** marker: {fts_result[:200]}"
    assert "*Session:" in fts_result, f"FTS result missing *Session: marker: {fts_result[:200]}"

    # Now force the grep path by removing the DB
    db_path = tr._db_path()
    if db_path.exists():
        db_path.unlink()

    # Point session dirs at a directory containing the fixture
    sessions_dir = tmp_path / "sessions" / "proj"
    sessions_dir.mkdir(parents=True)
    import shutil
    shutil.copy(_REAL_SESSION_FIXTURE, sessions_dir / _REAL_SESSION_FIXTURE.name)

    monkeypatch.setattr(ss, "SESSIONS_DIRS", [tmp_path / "sessions"])
    monkeypatch.setattr(tr, "SESSIONS_DIRS", [tmp_path / "sessions"])

    grep_result = tr.transcript_search("memory", limit=3)

    if grep_result == "No matching transcripts found":
        pytest.skip("Fixture produced no grep results for 'memory'")

    # Both must have same structural markers
    for marker in ("**Q:**", "**A:**", "*Session:"):
        assert marker in grep_result, f"Grep result missing {marker!r}: {grep_result[:200]}"


def test_fts_index_session_idempotent(fts_env):
    """Calling index_session twice on the same file produces the same row count (no duplicates)."""
    import sqlite3
    import memem.transcripts as tr

    tr.index_session(_REAL_SESSION_FIXTURE)

    db_path = tr._db_path()
    conn = sqlite3.connect(str(db_path))
    count_after_first = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE session_id = ?",
        (_REAL_SESSION_FIXTURE.stem,),
    ).fetchone()[0]
    conn.close()

    assert count_after_first >= 1, f"Expected >= 1 row after first index, got {count_after_first}"

    tr.index_session(_REAL_SESSION_FIXTURE)  # second call — should DELETE+INSERT (same count)

    conn = sqlite3.connect(str(db_path))
    count_after_second = conn.execute(
        "SELECT COUNT(*) FROM transcripts WHERE session_id = ?",
        (_REAL_SESSION_FIXTURE.stem,),
    ).fetchone()[0]
    conn.close()

    assert count_after_second == count_after_first, (
        f"Expected same row count after double-index, got {count_after_first} then {count_after_second}"
    )


def test_fts_per_turn_granularity(fts_env, tmp_path):
    """FTS path returns more than one distinct Q: block for a multi-turn session.

    Granularity contract: a session with N>1 Q/A pairs must produce N distinct
    **Q:** entries (not a single merged blob) from the FTS path.
    """
    import memem.transcripts as tr

    # Build a multi-turn session with 3 distinct exchanges
    records = [
        {"type": "user", "message": {"content": "Question about alpha"}, "timestamp": "2026-01-01T00:00:01Z"},
        {"type": "assistant", "message": {"content": "Answer about alpha"}, "timestamp": "2026-01-01T00:00:02Z"},
        {"type": "user", "message": {"content": "Question about beta"}, "timestamp": "2026-01-01T00:00:03Z"},
        {"type": "assistant", "message": {"content": "Answer about beta"}, "timestamp": "2026-01-01T00:00:04Z"},
        {"type": "user", "message": {"content": "Question about gamma"}, "timestamp": "2026-01-01T00:00:05Z"},
        {"type": "assistant", "message": {"content": "Answer about gamma"}, "timestamp": "2026-01-01T00:00:06Z"},
    ]
    session = tmp_path / "multi_turn.jsonl"
    _write_jsonl(session, records)

    tr.index_session(session)

    # Search for a word present in all turns
    result = tr.transcript_search("Question", limit=10)
    assert result != "No matching transcripts found", f"Expected results, got: {result!r}"

    # Count **Q:** occurrences — must be > 1 (per-pair granularity)
    q_count = result.count("**Q:**")
    assert q_count > 1, (
        f"Expected more than 1 **Q:** block (per-pair granularity), got {q_count}.\n"
        f"Result:\n{result}"
    )


def test_fts_turn_with_blank_line_not_dropped(fts_env, tmp_path):
    """A Q/A turn whose text contains a blank line must NOT be dropped by the FTS path.

    The old "\n\n"-split reconstruction silently dropped such turns. With per-pair
    storage (user_text/assistant_text stored directly as UNINDEXED columns), blank
    lines in text are preserved.
    """
    import memem.transcripts as tr

    # A user message whose text contains a blank line
    records = [
        {
            "type": "user",
            "message": {"content": "First paragraph of question.\n\nSecond paragraph of question."},
            "timestamp": "2026-01-01T00:00:01Z",
        },
        {
            "type": "assistant",
            "message": {"content": "Answer with\n\nblank lines in it too."},
            "timestamp": "2026-01-01T00:00:02Z",
        },
    ]
    session = tmp_path / "blank_line_turn.jsonl"
    _write_jsonl(session, records)

    tr.index_session(session)

    # Search for a word from the second paragraph (after the blank line)
    result = tr.transcript_search("paragraph", limit=5)
    assert result != "No matching transcripts found", (
        f"Turn with blank line was dropped by FTS path; got: {result!r}"
    )
    assert "**Q:**" in result, f"Missing **Q:** marker: {result}"
    assert "paragraph" in result.lower(), f"Content with blank line not preserved: {result}"


def test_grep_fallback_bounded(fts_env, tmp_path, monkeypatch):
    """Grep fallback respects MAX_FALLBACK_FILES cap and returns without hanging.

    With cap=3 and 10 total files, the fallback must stop after 3 and emit a
    structured warning. Because structlog writes to stdout (not Python logging),
    we verify the cap is enforced via observable behavior: the function returns
    (no hang) and scans at most `cap` files (visible via monkeypatching the
    warning emitter to capture calls).
    """
    import memem.transcripts as tr
    import memem.session_state as ss

    # Create 10 JSONL files each large enough to pass the 5000-byte size floor.
    sessions_dir = tmp_path / "sessions" / "proj"
    sessions_dir.mkdir(parents=True)

    padding = "x" * 5100
    for i in range(10):
        records = [
            {"type": "user", "message": {"content": f"unique_needle_{i} {padding}"}},
            {"type": "assistant", "message": {"content": f"response_{i}"}},
        ]
        p = sessions_dir / f"session_{i:04d}.jsonl"
        _write_jsonl(p, records)

    monkeypatch.setattr(ss, "SESSIONS_DIRS", [tmp_path / "sessions"])
    monkeypatch.setattr(tr, "SESSIONS_DIRS", [tmp_path / "sessions"])

    # Patch the cap down to 3 so we don't need 500 files
    monkeypatch.setattr(tr, "MAX_FALLBACK_FILES", 3)

    # Capture structlog warning calls via monkeypatching the module-level log
    warnings_emitted: list[dict] = []

    class _CapturingLogger:
        def warning(self, msg, **kw):
            warnings_emitted.append({"msg": msg, **kw})
        def info(self, msg, **kw):
            pass

    monkeypatch.setattr(tr, "log", _CapturingLogger())

    # Ensure FTS DB is absent so grep fallback is used
    db_path = tr._db_path()
    if db_path.exists():
        db_path.unlink()

    result = tr.transcript_search("unique_needle", limit=10)

    # Must return without hanging (already done if we got here)
    assert isinstance(result, str), "transcript_search must return str"

    # Must have emitted the truncation warning via log.warning
    truncation_warned = any(
        "truncated" in w.get("msg", "") or "cap" in str(w.get("reason", "")).lower()
        for w in warnings_emitted
    )
    assert truncation_warned, (
        f"Expected a truncation warning from log.warning(), got: {warnings_emitted}"
    )
