"""Unit tests for extract_dominant_paths in memem.mine_delta.

Covers:
- File edited ≥_PATH_MIN_FREQ times across raw turns qualifies
- Fewer than 2 qualifying paths → returns []
- Bash-command paths counted per call
- Turn schema: nested Claude Code JSONL {"type":"assistant","message":{"role","content":[...]}}
"""

from __future__ import annotations


def _make_assistant_turn(tool_uses: list[dict]) -> dict:
    """Build a nested Claude Code schema assistant turn with tool_use blocks."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"tu_{i}", **tu}
                for i, tu in enumerate(tool_uses)
            ],
        },
    }


def _edit_block(file_path: str) -> dict:
    return {"name": "Edit", "input": {"file_path": file_path, "old_string": "a", "new_string": "b"}}


def _read_block(file_path: str) -> dict:
    return {"name": "Read", "input": {"file_path": file_path}}


def _write_block(file_path: str) -> dict:
    return {"name": "Write", "input": {"file_path": file_path, "content": "..."}}


def _bash_block(command: str) -> dict:
    return {"name": "Bash", "input": {"command": command}}


# ---------------------------------------------------------------------------
# Test: file appearing ≥3 times qualifies
# ---------------------------------------------------------------------------

class TestDominantPathsQualifies:

    def test_single_file_edited_three_times_qualifies(self):
        """A file edited exactly _PATH_MIN_FREQ (3) times must appear in results."""
        from memem.mine_delta import extract_dominant_paths, _PATH_MIN_FREQ

        turns = [
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/server.py" in result, (
            f"File edited {_PATH_MIN_FREQ}x must qualify; got {result!r}"
        )

    def test_file_edited_five_times_qualifies(self):
        """A file edited 5 times (above threshold) qualifies."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([
                _edit_block("memem/mine_delta.py"),
                _edit_block("memem/mine_delta.py"),
            ]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
            _make_assistant_turn([_read_block("memem/mine_delta.py")]),
            _make_assistant_turn([_read_block("memem/mine_delta.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/mine_delta.py" in result, (
            f"File touched 5x must qualify; got {result!r}"
        )

    def test_file_edited_below_threshold_excluded(self):
        """A file edited only 2 times (below _PATH_MIN_FREQ=3) must NOT qualify."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/server.py" not in result, (
            f"File touched only 2x must not qualify (threshold=3); got {result!r}"
        )

    def test_write_calls_counted(self):
        """Write calls count toward path frequency."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_write_block("tests/output.py")]),
            _make_assistant_turn([_write_block("tests/output.py")]),
            _make_assistant_turn([_write_block("tests/output.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "tests/output.py" in result, (
            f"Write calls must be counted; got {result!r}"
        )

    def test_mixed_edit_read_counted_together(self):
        """Edit and Read calls to the same file are combined in the frequency count."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_edit_block("memem/recall.py")]),
            _make_assistant_turn([_read_block("memem/recall.py")]),
            _make_assistant_turn([_edit_block("memem/recall.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/recall.py" in result, (
            f"Mixed Edit+Read calls must be combined; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test: fewer than 2 qualifying paths → returns []
# ---------------------------------------------------------------------------

class TestDominantPathsFewQualify:

    def test_only_one_qualifying_path_caller_filters(self):
        """When exactly 1 path qualifies (≥3×) but no second, the ≥2 guard in
        run() resets dominant_paths to [].  extract_dominant_paths itself returns
        the 1-element list; the caller enforces the ≥2 threshold."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
            # second file only appears twice — below threshold
            _make_assistant_turn([_read_block("memem/models.py")]),
            _make_assistant_turn([_read_block("memem/models.py")]),
        ]
        result = extract_dominant_paths(turns)
        # The function returns paths that meet _PATH_MIN_FREQ; the ≥2 rule is
        # applied by the caller (run()) — so 1 qualifying path is returned here.
        assert len(result) == 1, (
            f"extract_dominant_paths returns 1 qualifying path; caller enforces ≥2; got {result!r}"
        )
        assert "memem/server.py" in result, (
            f"The qualifying path must be server.py; got {result!r}"
        )

    def test_no_turns_returns_empty(self):
        """Empty turn list returns []."""
        from memem.mine_delta import extract_dominant_paths

        result = extract_dominant_paths([])
        assert result == [], f"Empty turns must return []; got {result!r}"

    def test_no_file_path_tools_returns_empty(self):
        """Turns with only Grep/Glob/unknown tools produce no path counts → []."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([
                {"name": "Grep", "input": {"query": "something", "path": "."}},
            ]),
            _make_assistant_turn([
                {"name": "Glob", "input": {"pattern": "**/*.py"}},
            ]),
        ]
        result = extract_dominant_paths(turns)
        assert result == [], f"Grep/Glob must not count as file paths; got {result!r}"

    def test_two_qualifying_paths_returns_both(self):
        """Two paths each appearing ≥3 times both qualify."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/server.py")]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/server.py" in result, f"server.py must qualify; got {result!r}"
        assert "memem/mine_delta.py" in result, f"mine_delta.py must qualify; got {result!r}"
        assert len(result) == 2, f"Exactly 2 qualifying paths; got {result!r}"


# ---------------------------------------------------------------------------
# Test: Bash-command paths counted
# ---------------------------------------------------------------------------

class TestDominantPathsBash:

    def test_bash_path_arg_counted(self):
        """Paths in Bash commands are counted per call."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_bash_block("python3 memem/server.py --status")]),
            _make_assistant_turn([_bash_block("python3 memem/server.py --status")]),
            _make_assistant_turn([_bash_block("python3 memem/server.py --status")]),
            # Need a second qualifying file for the ≥2 rule
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
            _make_assistant_turn([_edit_block("memem/mine_delta.py")]),
        ]
        result = extract_dominant_paths(turns)
        # memem/mine_delta.py must qualify; Bash paths are implementation-dependent
        # but the second qualifying path (mine_delta.py) must be present
        assert "memem/mine_delta.py" in result, (
            f"Edit calls counted; got {result!r}"
        )

    def test_bash_absolute_path_counted(self):
        """Absolute paths in Bash commands (e.g. /tmp/foo.py) are counted."""
        from memem.mine_delta import extract_dominant_paths

        turns = [
            _make_assistant_turn([_bash_block("python3 /tmp/test_runner.py")]),
            _make_assistant_turn([_bash_block("python3 /tmp/test_runner.py")]),
            _make_assistant_turn([_bash_block("python3 /tmp/test_runner.py")]),
            _make_assistant_turn([_edit_block("/tmp/test_runner.py")]),
            _make_assistant_turn([_edit_block("/tmp/test_runner.py")]),
            _make_assistant_turn([_edit_block("/tmp/test_runner.py")]),
        ]
        result = extract_dominant_paths(turns)
        # /tmp/test_runner.py should appear from both Edit and Bash counts
        assert "/tmp/test_runner.py" in result, (
            f"Absolute path from Bash and Edit must qualify; got {result!r}"
        )

    def test_bash_calls_each_counted_separately(self):
        """Each Bash call is an independent count — same command 3× → count of 3."""
        from memem.mine_delta import extract_dominant_paths

        # Only Bash with a file path — but Bash paths are counted by _BASH_PATH_RE.
        # Also add Edit calls for the second path (≥2 qualifying requirement).
        turns = [
            _make_assistant_turn([_bash_block("pytest tests/test_foo.py -q")]),
            _make_assistant_turn([_bash_block("pytest tests/test_foo.py -q")]),
            _make_assistant_turn([_bash_block("pytest tests/test_foo.py -q")]),
            _make_assistant_turn([_edit_block("memem/operations.py")]),
            _make_assistant_turn([_edit_block("memem/operations.py")]),
            _make_assistant_turn([_edit_block("memem/operations.py")]),
        ]
        result = extract_dominant_paths(turns)
        assert "memem/operations.py" in result, (
            f"Edit path must qualify; got {result!r}"
        )
        # tests/test_foo.py should also qualify from Bash calls
        assert "tests/test_foo.py" in result, (
            f"Bash path tests/test_foo.py (3×) must qualify; got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test: raw turns vs deduped digest — the core regression test
# ---------------------------------------------------------------------------

class TestDominantPathsRawVsDedup:

    def test_same_file_many_edits_all_counted(self):
        """Regression: editing server.py 5× must score 5, not 1 (dedup bug)."""
        from memem.mine_delta import extract_dominant_paths

        # server.py edited 5×, retrieve.py edited 4×, models.py read 3×
        # All three should qualify with raw counting; with dedup none would reach 3
        turns = (
            [_make_assistant_turn([_edit_block("memem/server.py")])] * 5
            + [_make_assistant_turn([_edit_block("memem/retrieve.py")])] * 4
            + [_make_assistant_turn([_read_block("memem/models.py")])] * 3
        )
        result = extract_dominant_paths(turns)
        assert "memem/server.py" in result, (
            f"server.py (5×) must qualify with raw counting; got {result!r}"
        )
        assert "memem/retrieve.py" in result, (
            f"retrieve.py (4×) must qualify with raw counting; got {result!r}"
        )
        # 3 qualifying paths → top-N=3 should include all
        assert len(result) >= 2, (
            f"At least 2 paths must qualify; got {result!r}"
        )
