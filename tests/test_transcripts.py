"""Tests for the canonical parse_jsonl_session in memem/transcripts.py."""
import json
from pathlib import Path


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
