"""Tests for per-session episode auto-emission in memem.mine_delta."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_mine_delta.py)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 6) -> list[dict]:
    """Generate synthetic turns with enough text to pass the trivial-delta check."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append({
            "role": role,
            "text": (
                f"This is turn {i} with substantive content about the codebase architecture "
                f"and important design decisions that should be remembered. "
                f"The function uses a callback pattern and returns a list of dicts. " * 2
            ),
        })
    return turns


# ---------------------------------------------------------------------------
# Fixtures (replicates mine_env from test_mine_delta.py)
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

    import memem.models as _models
    importlib.reload(_models)
    import memem.obsidian_store as _store
    importlib.reload(_store)

    yield {
        "state_dir": state_dir,
        "vault_dir": vault_dir,
        "tmp_path": tmp_path,
    }

    importlib.reload(_models)
    importlib.reload(_store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubstantiveSessionEmitsEpisode:
    """Substantive session (>=1 regular memory, >=3 turns) → episode emitted."""

    def test_substantive_session_emits_episode(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        # Mock regular Haiku extraction to return 1 memory
        canned_memories = [
            {
                "title": "Callback pattern",
                "project": "cortex-plugin",
                "content": "The function uses a callback pattern and returns a list of dicts.",
                "importance": 3,
            }
        ]

        def _fake_extract(text: str, context_hint: str = "") -> list[dict]:
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        # Mock the episode Haiku subprocess call
        EPISODE_SUMMARY = (
            "The user worked on the cortex-plugin codebase, focusing on a callback pattern "
            "implementation. The team decided to use a list-of-dicts return type for the "
            "function. No open questions remained after the session concluded."
        )

        import subprocess
        original_run = subprocess.run

        def _fake_subprocess_run(cmd, **kwargs):
            # Only intercept episode calls (those using _EPISODE_HAIKU_SYSTEM)
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = EPISODE_SUMMARY
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _fake_subprocess_run)
        # Also patch subprocess in mine_delta module scope
        monkeypatch.setattr(md.subprocess, "run", _fake_subprocess_run)

        session_id = "test-episode-session-abc"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Verify vault has exactly 2 .md files
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        md_files = list(memories_dir.glob("*.md"))
        assert len(md_files) == 2, (
            f"Expected exactly 2 .md files (1 regular + 1 episode), found {len(md_files)}: "
            f"{[f.name for f in md_files]}"
        )

        # Find the episode file
        episode_files = []
        for f in md_files:
            content = f.read_text()
            if "type:episodic" in content:
                episode_files.append(f)

        assert len(episode_files) == 1, (
            f"Expected exactly 1 episode file with 'type:episodic' tag, found {len(episode_files)}"
        )

        episode_content = episode_files[0].read_text()
        assert f"Session {session_id[:8]}" in episode_content, (
            f"Episode title should start with 'Session {session_id[:8]}', got:\n{episode_content[:300]}"
        )


class TestTrivialSessionNoEpisode:
    """Trivial/ack-only session → no episode emitted, no Haiku calls."""

    def test_trivial_session_no_episode(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        haiku_called = [False]

        def _fake_extract(text: str, context_hint: str = "") -> list[dict]:
            haiku_called[0] = True
            return []

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        episode_called = [False]

        def _fake_emit_episode(session_id, turns, first_user_msg):
            episode_called[0] = True
            return False

        monkeypatch.setattr(md, "_emit_session_episode", _fake_emit_episode)

        session_id = "test-trivial-ep-sess"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        # Short ack-only delta — too short and ack-only so trivial check fires
        _write_jsonl(transcript, [{"role": "user", "text": "ok"}])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Neither Haiku extraction nor episode emission should be called
        assert not haiku_called[0], "Haiku extraction should NOT be called for trivial delta"
        assert not episode_called[0], "Episode emission should NOT be called for trivial delta"

        # No .md files in vault
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        md_files = list(memories_dir.glob("*.md"))
        assert len(md_files) == 0, (
            f"Expected 0 .md files for trivial session, found {len(md_files)}"
        )


class TestEpisodeFailureDoesntBreakRun:
    """Episode emission failure → regular memories still written, run() doesn't crash."""

    def test_episode_failure_doesnt_break_run(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        # Mock regular Haiku to return 1 memory
        canned_memories = [
            {
                "title": "Architecture decision",
                "project": "cortex-plugin",
                "content": "Decided to use async-first design for the new pipeline module.",
                "importance": 4,
            }
        ]

        def _fake_extract(text: str, context_hint: str = "") -> list[dict]:
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)

        # Make _emit_session_episode raise an exception
        def _raising_emit(session_id, turns, first_user_msg):
            raise RuntimeError("Simulated episode emission failure")

        monkeypatch.setattr(md, "_emit_session_episode", _raising_emit)

        session_id = "test-episode-fail-sess"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        # run() should not raise
        md.run(session_id=session_id, transcript_path=str(transcript))

        # Regular memory should still have been written
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        md_files = list(memories_dir.glob("*.md"))
        assert len(md_files) >= 1, (
            f"Expected at least 1 regular memory file despite episode failure, found {len(md_files)}"
        )

        # Verify it's the regular memory (not an episode)
        regular_files = []
        for f in md_files:
            content = f.read_text()
            if "Architecture decision" in content or "architecture decision" in content.lower():
                regular_files.append(f)
        assert len(regular_files) >= 1, (
            "Expected the regular 'Architecture decision' memory to be present"
        )
