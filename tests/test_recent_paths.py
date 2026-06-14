"""Tests for memem.transcripts.recent_session_paths (m5 path-scope feature)."""
import json
from pathlib import Path

import pytest


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write a list of dicts as JSONL (creates parent dirs if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _make_tool_use_block(name: str, **inp) -> dict:
    return {"type": "tool_use", "name": name, "id": "x", "input": inp}


def _make_assistant_record(tool_blocks: list[dict], text: str = "ok") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                *tool_blocks,
            ],
        },
    }


def _make_user_record(text: str = "hi") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": text,
        },
    }


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    """Monkeypatch SESSIONS_DIRS to a controlled tmp dir.

    NEVER scans the real ~/.claude dirs — prevents hangs on large session dirs.
    """
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()

    import memem.session_state as _ss
    import memem.transcripts as _tr

    monkeypatch.setattr(_ss, "SESSIONS_DIRS", [sessions_root])
    monkeypatch.setattr(_tr, "SESSIONS_DIRS", [sessions_root])

    return sessions_root


# ---------------------------------------------------------------------------
# Core behaviour tests
# ---------------------------------------------------------------------------

def test_missing_session_returns_empty(isolated_sessions):
    """Non-existent session_id → []."""
    from memem.transcripts import recent_session_paths

    result = recent_session_paths("no-such-session-id-xyz")
    assert result == []


def test_empty_session_id_returns_empty(isolated_sessions):
    """Empty string session_id → []."""
    from memem.transcripts import recent_session_paths

    result = recent_session_paths("")
    assert result == []


def test_basic_read_edit_write_extraction(isolated_sessions):
    """Paths from Read/Edit/Write tool calls are extracted."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-abc"
    proj_dir.mkdir()

    session_id = "test-session-read-edit"
    records = [
        _make_assistant_record([
            _make_tool_use_block("Read", file_path="/home/user/foo.py"),
            _make_tool_use_block("Edit", file_path="/home/user/bar.py"),
            _make_tool_use_block("Write", file_path="/home/user/baz.py"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=10)
    assert "/home/user/foo.py" in result
    assert "/home/user/bar.py" in result
    assert "/home/user/baz.py" in result


def test_notebook_edit_extraction(isolated_sessions):
    """NotebookEdit file_path is extracted."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-nb"
    proj_dir.mkdir()
    session_id = "session-notebook"
    records = [
        _make_assistant_record([
            _make_tool_use_block("NotebookEdit", file_path="/home/user/analysis.ipynb"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=5)
    assert "/home/user/analysis.ipynb" in result


def test_bash_arg_extraction(isolated_sessions):
    """File paths in Bash command first-line args are extracted."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-bash"
    proj_dir.mkdir()
    session_id = "session-bash"
    records = [
        _make_assistant_record([
            _make_tool_use_block("Bash", command="/usr/bin/python3 /home/user/run.py --flag"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=5)
    # The bash extractor grabs leading path-like args
    assert any("/home/user/run.py" in p or "/usr/bin/python3" in p for p in result), \
        f"Expected a path from Bash arg, got: {result}"


def test_recency_ordering(isolated_sessions):
    """Most recently accessed files appear first (walk newest-first)."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-order"
    proj_dir.mkdir()
    session_id = "session-order"

    # older message has file A, newer message has file B
    records = [
        _make_user_record("start"),
        _make_assistant_record([
            _make_tool_use_block("Read", file_path="/home/user/old.py"),
        ]),
        _make_user_record("next"),
        _make_assistant_record([
            _make_tool_use_block("Edit", file_path="/home/user/new.py"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=5)
    assert result[0] == "/home/user/new.py", f"Newest file should be first, got: {result}"
    assert "/home/user/old.py" in result


def test_deduplication_keeps_newest_occurrence(isolated_sessions):
    """Each path appears at most once; first occurrence walking newest-first wins."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-dedup"
    proj_dir.mkdir()
    session_id = "session-dedup"

    # same path appears in older and newer message
    records = [
        _make_assistant_record([
            _make_tool_use_block("Read", file_path="/home/user/dup.py"),
        ]),
        _make_assistant_record([
            _make_tool_use_block("Edit", file_path="/home/user/dup.py"),
            _make_tool_use_block("Read", file_path="/home/user/other.py"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=10)
    # Each path appears exactly once
    assert result.count("/home/user/dup.py") == 1
    # The newer occurrence (index 0 from newest-first walk) wins
    assert result[0] == "/home/user/dup.py"


def test_limit_cap(isolated_sessions):
    """Result is capped at `limit` distinct paths."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-limit"
    proj_dir.mkdir()
    session_id = "session-limit"

    blocks = [
        _make_tool_use_block("Read", file_path=f"/home/user/file{i}.py")
        for i in range(10)
    ]
    records = [_make_assistant_record(blocks)]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=3)
    assert len(result) == 3


def test_malformed_jsonl_returns_empty(isolated_sessions):
    """Corrupt JSONL (all-garbage) returns [] without raising."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-bad"
    proj_dir.mkdir()
    session_id = "session-malformed"
    (proj_dir / f"{session_id}.jsonl").write_text(
        "not json at all\n{broken\n{{invalid}}\n", encoding="utf-8"
    )

    result = recent_session_paths(session_id)
    assert result == []


def test_session_with_no_tool_use_returns_empty(isolated_sessions):
    """Session with only text messages (no tool calls) returns []."""
    from memem.transcripts import recent_session_paths

    proj_dir = isolated_sessions / "project-notools"
    proj_dir.mkdir()
    session_id = "session-notools"
    records = [
        _make_user_record("hello"),
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "world"},
        },
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id)
    assert result == []


def test_rglob_finds_nested_session(isolated_sessions):
    """Session file nested two levels deep is found via rglob."""
    from memem.transcripts import recent_session_paths

    nested_dir = isolated_sessions / "a" / "b" / "c"
    nested_dir.mkdir(parents=True)
    session_id = "nested-session"
    records = [
        _make_assistant_record([
            _make_tool_use_block("Read", file_path="/home/user/nested.py"),
        ]),
    ]
    _write_jsonl(nested_dir / f"{session_id}.jsonl", records)

    result = recent_session_paths(session_id, limit=5)
    assert "/home/user/nested.py" in result


# ---------------------------------------------------------------------------
# Integration: active_memory_slice wiring test
# ---------------------------------------------------------------------------

def test_active_memory_slice_passes_paths_context_when_session_has_files(
    isolated_sessions, monkeypatch, tmp_path
):
    """When a session has recent file ops, active_memory_slice passes a non-empty
    paths_context to retrieve().

    Drives the REAL active_memory_slice tool registered in _build_mcp() by:
    1. Patching memem.recall._get_current_session_id (imported by _build_mcp before
       the tool closure captures it) so the tool sees our known session_id.
    2. Injecting fake memem.retrieve and memem.render modules (lazy-imported inside
       the tool body) so retrieve() calls are captured and render doesn't need the vault.
    3. Invoking the registered tool via asyncio.run(tool.run(...)).
    4. Asserting captured['paths_context'] is non-empty and contains the expected path.
    """
    import asyncio
    import sys
    import types

    # Write a session with a Read tool call into isolated_sessions
    proj_dir = isolated_sessions / "proj-integration"
    proj_dir.mkdir()
    session_id = "integration-session-abc123"
    records = [
        _make_assistant_record([
            _make_tool_use_block("Read", file_path="/home/user/memem/server.py"),
        ]),
    ]
    _write_jsonl(proj_dir / f"{session_id}.jsonl", records)

    # Patch _get_current_session_id at the name server.py resolves at call time.
    # _build_mcp() does `from memem.recall import _get_current_session_id` so we
    # patch memem.recall._get_current_session_id before _build_mcp() runs.
    import memem.recall as _recall_module
    monkeypatch.setattr(_recall_module, "_get_current_session_id", lambda: session_id)
    # SESSIONS_DIRS isolation already applied by isolated_sessions fixture via
    # monkeypatch on memem.transcripts.SESSIONS_DIRS.

    # Capture calls to retrieve
    captured: dict = {}

    def fake_retrieve(query, k=8, log_call_type=None, scope_id="", paths_context=None, writeback=True):
        captured["paths_context"] = paths_context
        return []

    # Inject fake retrieve and render modules so _build_mcp's lazy imports hit them.
    fake_retrieve_mod = types.ModuleType("memem.retrieve")
    fake_retrieve_mod.retrieve = fake_retrieve
    fake_render_mod = types.ModuleType("memem.render")
    fake_render_mod.render_slice = lambda q, r, w: "# slice"

    sys.modules["memem.retrieve"] = fake_retrieve_mod
    sys.modules["memem.render"] = fake_render_mod

    try:
        # Build MCP instance with our patched modules in place.
        # Import _build_mcp after patching sys.modules so the lazy imports inside
        # tool bodies pick up the fake modules.
        from memem.server import _build_mcp
        mcp = _build_mcp()

        # Resolve the registered active_memory_slice tool
        tool = mcp._tool_manager._tools["active_memory_slice"]

        # Tool.run is a coroutine — drive it synchronously via asyncio.run()
        asyncio.run(tool.run({"query": "what files are open"}))

        # The wiring should have auto-derived paths_context from the session
        assert "paths_context" in captured, "retrieve() was never called"
        assert captured["paths_context"] is not None, (
            "paths_context was not auto-derived (got None); "
            f"session_id={session_id}, isolated_sessions={isolated_sessions}"
        )
        assert "/home/user/memem/server.py" in captured["paths_context"], (
            f"Expected path not in paths_context: {captured['paths_context']}"
        )

    finally:
        sys.modules.pop("memem.retrieve", None)
        sys.modules.pop("memem.render", None)
