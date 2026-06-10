"""Tests for _build_tool_digest in memem.mine_delta (C3).

Covers:
- assistant turn with mixed text + tool_use blocks (Edit, Bash, Read, NotebookEdit, Grep)
- user turn with tool_result: error output captured, non-error skipped
- deduplication (same entry appears only once)
- 600-char cap with truncation marker
- empty case (no tool blocks → '')
- e2e: run() appends '=== TOOL TRACE (digest) ===' to the text passed to
  extract_from_text (via monkeypatch capture)
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_assistant_turn(blocks: list[dict]) -> dict:
    """Nested Claude Code schema assistant turn with arbitrary content blocks."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": blocks,
        },
    }


def _make_user_turn(blocks: list[dict]) -> dict:
    """Nested Claude Code schema user turn with arbitrary content blocks."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": blocks,
        },
    }


def _make_user_text_turn(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def _make_assistant_text_turn(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


# ---------------------------------------------------------------------------
# Unit tests for _build_tool_digest
# ---------------------------------------------------------------------------


class TestBuildToolDigestEmpty:
    """Empty / no tool blocks → empty string."""

    def test_empty_turns(self):
        from memem.mine_delta import _build_tool_digest

        result = _build_tool_digest([])
        assert result == "", f"Expected '' for empty turns, got {result!r}"

    def test_turns_with_only_text_blocks(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_user_text_turn("What is the weather?"),
            _make_assistant_text_turn("I cannot check the weather."),
        ]
        result = _build_tool_digest(turns)
        assert result == "", f"Expected '' for text-only turns, got {result!r}"

    def test_turns_with_legacy_flat_schema(self):
        """Legacy flat-schema turns (no .message key) produce empty digest."""
        from memem.mine_delta import _build_tool_digest

        turns = [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "hi"},
        ]
        result = _build_tool_digest(turns)
        assert result == "", f"Expected '' for legacy flat turns, got {result!r}"


class TestBuildToolDigestToolUse:
    """tool_use blocks: salient args extracted for Edit, Bash, Read, NotebookEdit, Grep."""

    def test_edit_block_extracts_file_path(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "text", "text": "I will edit the file."},
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Edit",
                    "input": {"file_path": "memem/mine_delta.py", "old_string": "x", "new_string": "y"},
                },
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Edit memem/mine_delta.py" in result, f"Expected Edit entry, got: {result!r}"

    def test_write_block_extracts_file_path(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu2", "name": "Write",
                 "input": {"file_path": "tests/test_foo.py", "content": "..."}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Write tests/test_foo.py" in result, f"Expected Write entry, got: {result!r}"

    def test_read_block_extracts_file_path(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu3", "name": "Read",
                 "input": {"file_path": "memem/models.py"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Read memem/models.py" in result, f"Expected Read entry, got: {result!r}"

    def test_notebook_edit_extracts_file_path(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu4", "name": "NotebookEdit",
                 "input": {"file_path": "notebooks/analysis.ipynb", "cell_id": "0"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "NotebookEdit notebooks/analysis.ipynb" in result, (
            f"Expected NotebookEdit entry, got: {result!r}"
        )

    def test_bash_block_first_line_truncated_at_80(self):
        from memem.mine_delta import _build_tool_digest

        # command has multiple lines; only first line, capped at 80 chars
        cmd = "pytest tests/ -q\nmore lines here that should not appear"
        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu5", "name": "Bash",
                 "input": {"command": cmd}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Bash: pytest tests/ -q" in result, f"Expected Bash entry, got: {result!r}"
        assert "more lines" not in result, f"Bash digest must not include lines after the first"

    def test_bash_long_first_line_truncated_at_80(self):
        from memem.mine_delta import _build_tool_digest

        cmd = "x" * 100  # single line, >80 chars
        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu6", "name": "Bash",
                 "input": {"command": cmd}},
            ])
        ]
        result = _build_tool_digest(turns)
        bash_line = [line for line in result.splitlines() if line.startswith("Bash: ")][0]
        assert len(bash_line) <= len("Bash: ") + 80, (
            f"Bash line should be capped at 80 chars of command, got: {len(bash_line)} chars"
        )

    def test_grep_block_extracts_query(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu7", "name": "Grep",
                 "input": {"query": "_build_tool_digest", "path": "."}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Grep: _build_tool_digest" in result, f"Expected Grep entry, got: {result!r}"

    def test_glob_block_extracts_pattern(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu8", "name": "Glob",
                 "input": {"pattern": "tests/test_*.py"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "Glob: tests/test_*.py" in result, f"Expected Glob entry, got: {result!r}"

    def test_unknown_tool_skipped(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu9", "name": "WebSearch",
                 "input": {"query": "python asyncio"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert result == "", f"Unknown tools should not appear in digest, got: {result!r}"

    def test_multiple_tools_multiple_turns(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "text", "text": "Let me read and then edit."},
                {"type": "tool_use", "id": "tu10", "name": "Read",
                 "input": {"file_path": "memem/foo.py"}},
                {"type": "tool_use", "id": "tu11", "name": "Edit",
                 "input": {"file_path": "memem/foo.py", "old_string": "a", "new_string": "b"}},
            ]),
            _make_user_turn([
                {"type": "tool_result", "tool_use_id": "tu10", "content": "file contents here"},
                {"type": "tool_result", "tool_use_id": "tu11", "content": "success"},
            ]),
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu12", "name": "Bash",
                 "input": {"command": "pytest tests/ -q"}},
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "Read memem/foo.py" in result
        assert "Edit memem/foo.py" in result
        assert "Bash: pytest tests/ -q" in result


class TestBuildToolDigestToolResult:
    """tool_result blocks: only error-looking first lines captured."""

    def test_error_result_captured(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "python3 -m pytest tests/"}},
            ]),
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": "ModuleNotFoundError: No module named 'memem'\nTraceback (most recent call last):\n  ...",
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "ERR:" in result, f"Expected ERR: entry for error output, got: {result!r}"
        assert "ModuleNotFoundError" in result

    def test_traceback_result_captured(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": "Traceback (most recent call last):\n  File 'x.py'\nValueError: bad arg",
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "ERR: Traceback" in result, f"Expected ERR: Traceback entry, got: {result!r}"

    def test_failed_result_captured(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": "FAILED tests/test_foo.py::TestBar::test_baz - AssertionError",
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "ERR: FAILED" in result, f"Expected ERR: FAILED entry, got: {result!r}"

    def test_non_error_result_skipped(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": "File written successfully.",
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert result == "", f"Non-error result should not appear in digest, got: {result!r}"

    def test_tool_result_with_list_content(self):
        """tool_result.content can be a list of blocks (not just a string)."""
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": [
                        {"type": "text", "text": "ImportError: cannot import name 'x'"},
                        {"type": "text", "text": "See traceback above"},
                    ],
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "ERR:" in result, f"List-content tool_result error should be captured, got: {result!r}"
        assert "ImportError" in result

    def test_error_only_in_first_200_chars_checked(self):
        """Error match only checks the first 200 chars of the content."""
        from memem.mine_delta import _build_tool_digest

        # Error keyword is hidden after 200 chars
        long_preamble = "a" * 250
        turns = [
            _make_user_turn([
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": long_preamble + "Error: something went wrong",
                },
            ]),
        ]
        result = _build_tool_digest(turns)
        assert result == "", (
            "Error beyond first 200 chars should NOT be captured, got: {result!r}"
        )


class TestBuildToolDigestDedup:
    """Duplicate entries are suppressed (first occurrence preserved, order stable)."""

    def test_dedupe_same_edit_twice(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Edit",
                 "input": {"file_path": "memem/mine_delta.py", "old_string": "a", "new_string": "b"}},
            ]),
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu2", "name": "Edit",
                 "input": {"file_path": "memem/mine_delta.py", "old_string": "c", "new_string": "d"}},
            ]),
        ]
        result = _build_tool_digest(turns)
        # Same file path → same entry, should appear exactly once
        count = result.split("\n").count("Edit memem/mine_delta.py")
        assert count == 1, f"Duplicate Edit entry should appear exactly once, got count={count}"

    def test_dedupe_same_bash_command(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "pytest tests/ -q"}},
                {"type": "tool_use", "id": "tu2", "name": "Bash",
                 "input": {"command": "pytest tests/ -q"}},
            ]),
        ]
        result = _build_tool_digest(turns)
        count = [line for line in result.split("\n") if "pytest tests/ -q" in line]
        assert len(count) == 1, f"Duplicate Bash entry should appear exactly once, got: {result!r}"

    def test_different_files_both_appear(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Edit",
                 "input": {"file_path": "memem/foo.py", "old_string": "a", "new_string": "b"}},
                {"type": "tool_use", "id": "tu2", "name": "Edit",
                 "input": {"file_path": "memem/bar.py", "old_string": "c", "new_string": "d"}},
            ]),
        ]
        result = _build_tool_digest(turns)
        assert "Edit memem/foo.py" in result
        assert "Edit memem/bar.py" in result


class TestBuildToolDigestCap:
    """Total digest capped at 600 chars; excess entries replaced with '…[+N more]'."""

    def test_cap_with_truncation_marker(self):
        from memem.mine_delta import _build_tool_digest, _TOOL_DIGEST_CAP

        # Create enough entries to exceed the cap
        # Each entry: "Edit path/to/filePath_XX.py" ≈ 30 chars + newline
        turns = [
            _make_assistant_turn([
                {
                    "type": "tool_use",
                    "id": f"tu{i}",
                    "name": "Edit",
                    "input": {"file_path": f"memem/module_{i:04d}.py",
                              "old_string": "a", "new_string": "b"},
                }
                for i in range(50)  # 50 distinct file paths → well above 600 chars
            ])
        ]
        result = _build_tool_digest(turns)
        assert len(result) <= _TOOL_DIGEST_CAP + 30, (
            f"Digest should be near cap, got {len(result)} chars"
        )
        assert "…[+" in result and "more]" in result, (
            f"Truncated digest must have '…[+N more]' marker, got: {result!r}"
        )

    def test_small_digest_no_truncation_marker(self):
        from memem.mine_delta import _build_tool_digest

        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "pytest tests/ -q"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert "…[+" not in result, (
            f"Small digest should not have truncation marker, got: {result!r}"
        )

    def test_digest_under_cap_not_truncated(self):
        from memem.mine_delta import _build_tool_digest, _TOOL_DIGEST_CAP

        # A single Bash entry is well under 600 chars
        turns = [
            _make_assistant_turn([
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "echo hello"}},
            ])
        ]
        result = _build_tool_digest(turns)
        assert len(result) < _TOOL_DIGEST_CAP, (
            f"Small digest should be under {_TOOL_DIGEST_CAP} chars, got {len(result)}"
        )


class TestBuildToolDigestE2ERun:
    """e2e: run() appends digest to text passed to extract_from_text (via monkeypatch).

    Uses the nested JSONL schema and canned subprocess (reconcile + episode)
    patterns from test_reconcile.py.
    """

    def _make_turns_with_tools(self) -> list[dict]:
        """Turns with substantive text + tool_use blocks (nested JSONL schema)."""
        return [
            # turn 0: user text
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "Please implement the _build_tool_digest function in mine_delta.py. "
                        "It should collect tool_use and tool_result blocks and produce a "
                        "compact digest capped at 600 characters. " * 3
                    ),
                },
            },
            # turn 1: assistant with text + tool_use
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will read the file first and then edit it."},
                        {
                            "type": "tool_use",
                            "id": "tu_read1",
                            "name": "Read",
                            "input": {"file_path": "memem/mine_delta.py"},
                        },
                    ],
                },
            },
            # turn 2: user with tool_result (non-error)
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_read1",
                            "content": "def run(session_id, transcript_path):\n    ...",
                        },
                    ],
                },
            },
            # turn 3: assistant with Edit + Bash
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Now I'll edit and run the tests."},
                        {
                            "type": "tool_use",
                            "id": "tu_edit1",
                            "name": "Edit",
                            "input": {"file_path": "memem/mine_delta.py",
                                      "old_string": "# placeholder", "new_string": "# real code"},
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_bash1",
                            "name": "Bash",
                            "input": {"command": "pytest tests/test_tool_digest.py -q\nmore lines"},
                        },
                    ],
                },
            },
            # turn 4: user with tool_results (one error, one success)
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_edit1",
                            "content": "File edited successfully",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_bash1",
                            "content": "FAILED tests/test_tool_digest.py::TestBuildToolDigest::test_cap - AssertionError",
                        },
                    ],
                },
            },
            # turn 5: assistant final text
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": (
                            "The tests now pass and the digest is properly capped. "
                            "The implementation is complete and working correctly. " * 3
                        )},
                    ],
                },
            },
        ]

    @pytest.fixture
    def mine_env(self, tmp_path, monkeypatch):
        """Isolated MEMEM_DIR and MEMEM_OBSIDIAN_VAULT for each test."""
        state_dir = tmp_path / ".memem"
        state_dir.mkdir()
        vault_dir = tmp_path / "obsidian-brain"
        (vault_dir / "memem" / "memories").mkdir(parents=True)

        monkeypatch.setenv("MEMEM_DIR", str(state_dir))
        monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
        monkeypatch.delenv("CORTEX_DIR", raising=False)
        monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

        import memem.models as _models
        importlib.reload(_models)
        import memem.obsidian_store as _store
        importlib.reload(_store)
        import memem.telemetry as _telemetry
        importlib.reload(_telemetry)
        import memem.mine_delta as _md
        importlib.reload(_md)

        yield {
            "state_dir": state_dir,
            "vault_dir": vault_dir,
            "tmp_path": tmp_path,
        }

        importlib.reload(_models)
        importlib.reload(_store)
        importlib.reload(_telemetry)
        importlib.reload(_md)

    def test_run_appends_digest_to_extraction_input(self, mine_env, monkeypatch):
        """run() must pass '=== TOOL TRACE (digest) ===' in the text given to
        extract_from_text when the turns contain tool_use blocks."""
        import memem.mine_delta as md

        # Capture the text passed to extract_from_text
        captured_texts: list[str] = []

        def _fake_extract(text: str, context_hint: str = "") -> list[dict]:
            captured_texts.append(text)
            return [{"title": "Digest test", "project": "cortex-plugin",
                     "content": "Fact extracted from session with tool trace.", "importance": 3}]

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        # Patch subprocess for reconcile + episode calls
        import subprocess as _subprocess
        original_run = _subprocess.run
        reconcile_resp = json.dumps([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new"}
        ])

        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
                class R:
                    returncode = 0
                    stdout = reconcile_resp
                    stderr = ""
                return R()
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class R:
                    returncode = 0
                    stdout = "Canned episode."
                    stderr = ""
                return R()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

        session_id = "test-digest-e2e-run"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, self._make_turns_with_tools())

        md.run(session_id=session_id, transcript_path=str(transcript))

        assert len(captured_texts) >= 1, (
            "extract_from_text must have been called at least once"
        )
        extraction_input = captured_texts[0]
        assert "=== TOOL TRACE (digest) ===" in extraction_input, (
            f"Extraction input must contain '=== TOOL TRACE (digest) ==='; "
            f"got (first 400 chars):\n{extraction_input[:400]}"
        )
        # Verify specific tool entries are in the digest
        assert "Read memem/mine_delta.py" in extraction_input, (
            "Read tool entry should appear in digest within extraction input"
        )
        assert "Edit memem/mine_delta.py" in extraction_input, (
            "Edit tool entry should appear in digest within extraction input"
        )
        assert "Bash: pytest tests/test_tool_digest.py -q" in extraction_input, (
            "Bash entry (first line only) should appear in digest"
        )

    def test_run_no_digest_when_no_tool_blocks(self, mine_env, monkeypatch):
        """run() must NOT append digest section when turns have no tool_use blocks."""
        import memem.mine_delta as md

        captured_texts: list[str] = []

        def _fake_extract(text: str, context_hint: str = "") -> list[dict]:
            captured_texts.append(text)
            return [{"title": "Plain text test", "project": "cortex-plugin",
                     "content": "Fact from plain conversation.", "importance": 3}]

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        import subprocess as _subprocess
        original_run = _subprocess.run
        reconcile_resp = json.dumps([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new"}
        ])

        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
                class R:
                    returncode = 0; stdout = reconcile_resp; stderr = ""
                return R()
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class R:
                    returncode = 0; stdout = "Episode."; stderr = ""
                return R()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

        # Turns with NO tool blocks — plain text only
        turns = []
        for i in range(6):
            role = "user" if i % 2 == 0 else "assistant"
            turns.append({
                "type": role,
                "message": {
                    "role": role,
                    "content": (
                        f"This is turn {i} discussing architecture and important design patterns. "
                        f"The codebase uses a modular approach with clear separation of concerns. " * 2
                    ),
                },
            })

        session_id = "test-no-digest-e2e-run"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, turns)

        md.run(session_id=session_id, transcript_path=str(transcript))

        assert len(captured_texts) >= 1, "extract_from_text must have been called"
        extraction_input = captured_texts[0]
        assert "=== TOOL TRACE (digest) ===" not in extraction_input, (
            "Digest section must NOT appear when turns have no tool blocks"
        )
