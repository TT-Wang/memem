"""Tests for the anti-recursion guard on memem's internal claude -p subprocess calls.

Part A: verifies that all 4 call sites pass MEMEM_HOOK_DISABLE=1 and start_new_session=True.
Part B: verifies that all 3 hook scripts early-exit with code 0 when MEMEM_HOOK_DISABLE=1 is set.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
HOOKS = {
    "auto-recall": REPO / "hooks" / "auto-recall.sh",
    "post-stop-attribution": REPO / "hooks" / "post-stop-attribution.sh",
    "session-start": REPO / "hooks" / "session-start.sh",
}


# ---------------------------------------------------------------------------
# Part A — subprocess.run call sites propagate anti-recursion env
# ---------------------------------------------------------------------------


class TestMergeMemoriesGuard:
    """_merge_memories in mining.py must pass MEMEM_HOOK_DISABLE=1 + start_new_session."""

    def test_env_and_session_flag(self):
        from memem import mining

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append(kwargs)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "merged content"
            m.stderr = ""
            return m

        with patch.object(mining.subprocess, "run", side_effect=fake_run):
            try:
                mining._merge_memories("existing", "new")
            except Exception:
                pass  # We only care about the call kwargs

        assert captured, "subprocess.run was not called"
        kw = captured[0]
        assert kw.get("start_new_session") is True, "start_new_session must be True"
        env = kw.get("env", {})
        assert env.get("MEMEM_HOOK_DISABLE") == "1", "MEMEM_HOOK_DISABLE must be '1' in env"


class TestRunHaikuKnowledgeExtractionGuard:
    """_run_haiku (knowledge extraction closure) in mining.py must pass guard params."""

    def test_env_and_session_flag(self):
        from memem import mining

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append(kwargs)
            m = MagicMock()
            m.returncode = 0
            # Return a valid JSON array so the extractor doesn't raise
            m.stdout = '[]'
            m.stderr = ""
            return m

        # _run_haiku is a closure inside _summarize_session_haiku; calling that
        # function triggers the subprocess.run we need to inspect.
        with patch.object(mining.subprocess, "run", side_effect=fake_run):
            try:
                mining._summarize_session_haiku(["user: hello", "assistant: hi"])
            except Exception:
                pass  # We only care that subprocess.run was called with correct args

        assert captured, "subprocess.run was not called in _summarize_session_haiku"
        kw = captured[0]
        assert kw.get("start_new_session") is True, "start_new_session must be True"
        env = kw.get("env", {})
        assert env.get("MEMEM_HOOK_DISABLE") == "1", "MEMEM_HOOK_DISABLE must be '1' in env"


class TestProceduralHaikuGuard:
    """Procedural Haiku call (_mine_procedural_suggestions) in mining.py must pass guard params."""

    def test_env_and_session_flag(self, tmp_path):
        from memem import mining

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append(kwargs)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "[]"
            m.stderr = ""
            return m

        # Create a fake CLAUDE.md in a fake session cwd so the function
        # doesn't bail out early before reaching the subprocess call.
        fake_cwd = tmp_path / "project"
        fake_cwd.mkdir()
        (fake_cwd / "CLAUDE.md").write_text("# Project instructions\n\nDo things.")
        # Create a fake .jsonl session file in fake_cwd to satisfy _detect_session_cwd
        fake_jsonl = tmp_path / "session.jsonl"
        fake_jsonl.write_text('{"type":"user","message":{"content":[{"type":"text","text":"hi"}]},"cwd":"' + str(fake_cwd) + '"}\n')

        with patch.object(mining.subprocess, "run", side_effect=fake_run):
            with patch.object(mining, "_detect_session_cwd", return_value=str(fake_cwd)):
                try:
                    mining._mine_procedural_suggestions(
                        ["user: hello", "assistant: hi"],
                        jsonl_path=fake_jsonl,
                        session_id="test-proc-guard",
                    )
                except Exception:
                    pass

        assert captured, "subprocess.run was not called in _mine_procedural_suggestions"
        kw = captured[0]
        assert kw.get("start_new_session") is True, "start_new_session must be True"
        env = kw.get("env", {})
        assert env.get("MEMEM_HOOK_DISABLE") == "1", "MEMEM_HOOK_DISABLE must be '1' in env"




# ---------------------------------------------------------------------------
# Part B — Hook scripts early-exit when MEMEM_HOOK_DISABLE=1
# ---------------------------------------------------------------------------


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Minimal environment for hook invocation."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "MEMEM_HOOK_DISABLE": "1",
        "MEMEM_DIR": str(tmp_path / ".memem"),
    }


def _no_files_created(tmp_path: Path) -> bool:
    """Assert that MEMEM_DIR is empty (no side effects)."""
    memem_dir = tmp_path / ".memem"
    if not memem_dir.exists():
        return True
    entries = list(memem_dir.iterdir())
    return len(entries) == 0


class TestAutoRecallHookGuard:
    def test_exits_zero_silently_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        result = subprocess.run(
            ["bash", str(HOOKS["auto-recall"])],
            input='{"session_id": "guard-test", "user_prompt": "hello world"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stderr}"
        assert result.stdout == "", f"Expected no stdout, got: {result.stdout!r}"

    def test_no_side_effects_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        subprocess.run(
            ["bash", str(HOOKS["auto-recall"])],
            input='{"session_id": "guard-test", "user_prompt": "hello world"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert _no_files_created(tmp_path), "Hook must not create files when MEMEM_HOOK_DISABLE=1"


class TestPostStopAttributionHookGuard:
    def test_exits_zero_silently_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        result = subprocess.run(
            ["bash", str(HOOKS["post-stop-attribution"])],
            input='{"session_id": "guard-test", "transcript_path": "/dev/null"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stderr}"
        assert result.stdout == "", f"Expected no stdout, got: {result.stdout!r}"

    def test_no_side_effects_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        subprocess.run(
            ["bash", str(HOOKS["post-stop-attribution"])],
            input='{"session_id": "guard-test", "transcript_path": "/dev/null"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert _no_files_created(tmp_path), "Hook must not create files when MEMEM_HOOK_DISABLE=1"


class TestSessionStartHookGuard:
    def test_exits_zero_silently_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        result = subprocess.run(
            ["bash", str(HOOKS["session-start"])],
            input='{"session_id": "guard-test", "cwd": "/tmp"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stderr}"
        assert result.stdout == "", f"Expected no stdout, got: {result.stdout!r}"

    def test_no_side_effects_when_disabled(self, tmp_path):
        env = _base_env(tmp_path)
        subprocess.run(
            ["bash", str(HOOKS["session-start"])],
            input='{"session_id": "guard-test", "cwd": "/tmp"}',
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert _no_files_created(tmp_path), "Hook must not create files when MEMEM_HOOK_DISABLE=1"
