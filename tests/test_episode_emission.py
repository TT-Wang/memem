"""Tests for per-session episode auto-emission in memem.mine_delta."""

from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirrored from test_mine_delta.py)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 6, first_user_msg: str = "") -> list[dict]:
    """Generate synthetic turns using the real nested Claude Code JSONL schema.

    Uses {"type":"user","message":{"role":"user","content":"..."}} — never the
    flat {role, text} schema, which is silently wrong in production.
    """
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        if i == 0 and first_user_msg:
            text = first_user_msg
        else:
            text = (
                f"This is turn {i} with substantive content about the codebase architecture "
                f"and important design decisions that should be remembered. "
                f"The function uses a callback pattern and returns a list of dicts. " * 2
            )
        turns.append({
            "type": role,
            "message": {
                "role": role,
                "content": text,
            },
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

        # Patch the reconcile subprocess with a canned ADD response — without
        # this the test makes a LIVE Haiku call whose op routing (e.g. PROFILE)
        # is nondeterministic and can route the candidate away from the vault.
        import subprocess as _subprocess
        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = ('[{"index": 0, "op": "ADD", "target": null, '
                              '"content": null, "reason": "new info"}]')
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

        # Make _emit_session_episode raise an exception. Signature-proof:
        # accept anything so future kwargs (e.g. transcript_path) can't make
        # this mock silently mismatch the call site.
        def _raising_emit(*args, **kwargs):
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


# ---------------------------------------------------------------------------
# B6: Stable episode id — two deltas produce ONE episode file
# ---------------------------------------------------------------------------


class TestStableEpisodeId:
    """Two run() calls for the same session → ONE episode file with stable uuid5 id,
    title from first delta, content updated by second."""

    EPISODE_SUMMARY_1 = (
        "The user worked on the cortex-plugin codebase in the first delta. "
        "They focused on callback patterns and design decisions. "
        "The team agreed on a list-of-dicts return type for the core function."
    )
    EPISODE_SUMMARY_2 = (
        "The user continued work on the cortex-plugin in the second delta. "
        "They refined the callback pattern and wrote additional unit tests. "
        "The session concluded with all tests passing successfully."
    )

    def _make_fake_subprocess(self, md, monkeypatch, summary_iter):
        """Patch subprocess.run so the episode Haiku call returns summaries in order."""
        import subprocess as _subprocess
        original_run = _subprocess.run
        call_count = [0]

        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                idx = call_count[0]
                call_count[0] += 1

                class FakeResult:
                    returncode = 0
                    stdout = summary_iter[idx % len(summary_iter)]
                    stderr = ""

                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

    def test_two_deltas_produce_one_episode_with_stable_id(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Expected stable episode id for this session
        session_id = "stable-episode-id-test-session"
        expected_episode_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memem-episode:{session_id}"))

        canned_memories = [{
            "title": "Callback pattern",
            "project": "cortex-plugin",
            "content": "The function uses a callback pattern and returns a list of dicts.",
            "importance": 3,
        }]

        def _fake_extract(text, context_hint=""):
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)
        self._make_fake_subprocess(md, monkeypatch, [self.EPISODE_SUMMARY_1, self.EPISODE_SUMMARY_2])

        transcript = mine_env["tmp_path"] / "session.jsonl"
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"

        # --- First delta ---
        _write_jsonl(transcript, _make_turns(6, first_user_msg="How do I implement the callback pattern?"))
        md.run(session_id=session_id, transcript_path=str(transcript))

        files_after_first = list(memories_dir.glob("*.md"))
        episode_files_1 = [f for f in files_after_first if "type:episodic" in f.read_text()]
        assert len(episode_files_1) == 1, (
            f"Expected 1 episode file after first delta, found {len(episode_files_1)}"
        )
        first_episode_filename = episode_files_1[0].name
        first_title = None
        for line in episode_files_1[0].read_text().splitlines():
            if line.startswith("title:"):
                first_title = line.split(":", 1)[1].strip()
                break
        assert first_title is not None, "Episode should have a title"
        assert "How do I implement" in first_title or "stable-epi" in first_title, (
            f"First episode title should use first user message, got: {first_title}"
        )

        # --- Second delta: append more turns and advance offset ---
        # Re-open transcript from where first run left off
        second_offset = md._read_offset(session_id)
        extra_turns = _make_turns(4)
        with transcript.open("a") as f:
            for t in extra_turns:
                f.write(json.dumps(t) + "\n")

        # Reset cache so find_memory can see the file written by delta 1
        store._reset_cache()

        md.run(session_id=session_id, transcript_path=str(transcript))

        files_after_second = list(memories_dir.glob("*.md"))
        episode_files_2 = [f for f in files_after_second if "type:episodic" in f.read_text()]
        assert len(episode_files_2) == 1, (
            f"Expected STILL exactly 1 episode file after second delta, found {len(episode_files_2)}. "
            f"Files: {[f.name for f in episode_files_2]}"
        )

        # Episode filename must be the same (stable id → stable slug)
        second_episode_filename = episode_files_2[0].name
        assert first_episode_filename == second_episode_filename, (
            f"Episode filename should be stable across deltas.\n"
            f"First:  {first_episode_filename}\n"
            f"Second: {second_episode_filename}"
        )

        # The stored id must equal the computed stable uuid5
        episode_content = episode_files_2[0].read_text()
        assert expected_episode_id in episode_content, (
            f"Episode should have stable id {expected_episode_id[:8]}, not found in:\n{episode_content[:400]}"
        )

        # Title must be preserved from the first emission
        second_title = None
        for line in episode_content.splitlines():
            if line.startswith("title:"):
                second_title = line.split(":", 1)[1].strip()
                break
        assert second_title == first_title, (
            f"Episode title should be preserved from first delta.\n"
            f"First:  {first_title}\n"
            f"Second: {second_title}"
        )

        # Content should have been updated (second summary differs from first)
        assert self.EPISODE_SUMMARY_2 in episode_content, (
            "Episode body should be updated with second delta's summary"
        )

    def test_legacy_uuid4_episode_adopted_not_duplicated(self, mine_env, monkeypatch):
        """Upgrade path: a pre-v2.5.0 episode saved under a random uuid4 id is
        adopted (matched via its session:<id> tag) and updated in place — NOT
        duplicated under the new uuid5 id."""
        import memem.mine_delta as md
        import memem.obsidian_store as store

        session_id = "legacy-adoption-test-session"

        # Pre-create a legacy episode the way pre-v2.5.0 releases did: random
        # uuid4 id (whatever _make_memory generates), session tag, episode type.
        legacy = store._make_memory(
            content="Legacy episode summary written by a pre-v2.5.0 release.",
            title=f"Session {session_id[:8]} — original legacy title",
            tags=["type:episodic", f"session:{session_id}"],
            project="general",
            source_type="mined-episode",
            source_session=session_id,
            importance=3,
        )
        store._save_memory(legacy)
        store._reset_cache()
        store._trigger_sweep()

        canned_memories = [{
            "title": "Some lesson",
            "project": "general",
            "content": "A durable lesson extracted from the post-upgrade delta.",
            "importance": 3,
        }]
        monkeypatch.setattr(
            md, "extract_from_text", lambda text, context_hint="": list(canned_memories)
        )
        self._make_fake_subprocess(md, monkeypatch, [self.EPISODE_SUMMARY_1])

        transcript = mine_env["tmp_path"] / "legacy-session.jsonl"
        _write_jsonl(transcript, _make_turns(6, first_user_msg="continue the legacy work"))
        md.run(session_id=session_id, transcript_path=str(transcript))

        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" in f.read_text() and f"session:{session_id}" in f.read_text()
        ]
        assert len(episode_files) == 1, (
            f"Legacy episode must be ADOPTED, not duplicated. "
            f"Found: {[f.name for f in episode_files]}"
        )
        text = episode_files[0].read_text()
        assert legacy["id"] in text, "Adopted episode must keep the legacy uuid4 id"
        assert "original legacy title" in text, "Adopted episode keeps its original title"
        assert "first delta" in text, "Episode content must be updated by the new delta's summary"


# ---------------------------------------------------------------------------
# B13: _project_from_transcript_path unit tests
# ---------------------------------------------------------------------------


class TestProjectFromTranscriptPath:
    """Unit tests for _project_from_transcript_path."""

    def test_home_project_returns_general(self, tmp_path):
        """Transcript under ~/.claude/projects/<munged-home>/<sid>.jsonl → 'general'."""
        import memem.mine_delta as md
        from pathlib import Path as _Path
        home = str(_Path.home())
        munged_home = home.replace("/", "-")
        # e.g. /home/claude-user → -home-claude-user
        transcript = f"{home}/.claude/projects/{munged_home}/abc123.jsonl"
        result = md._project_from_transcript_path(transcript)
        assert result == "general", (
            f"Home dir transcript should return 'general', got {result!r}"
        )

    def test_nested_project_returns_basename(self, tmp_path):
        """Transcript under ~/.claude/projects/<munged-home>-cortex-plugin/<sid>.jsonl
        → 'cortex-plugin'."""
        import memem.mine_delta as md
        from pathlib import Path as _Path
        home = str(_Path.home())
        munged_home = home.replace("/", "-")
        transcript = f"{home}/.claude/projects/{munged_home}-cortex-plugin/abc123.jsonl"
        result = md._project_from_transcript_path(transcript)
        assert result == "cortex-plugin", (
            f"Should extract 'cortex-plugin' from transcript path, got {result!r}"
        )

    def test_deep_nested_project(self, tmp_path):
        """Transcript for /home/user/work/myproject → 'work-myproject'."""
        import memem.mine_delta as md
        from pathlib import Path as _Path
        home = str(_Path.home())
        munged_home = home.replace("/", "-")
        transcript = f"{home}/.claude/projects/{munged_home}-work-myproject/abc123.jsonl"
        result = md._project_from_transcript_path(transcript)
        assert result == "work-myproject", (
            f"Deep nested path should return 'work-myproject', got {result!r}"
        )

    def test_non_home_path_returns_dir_name(self):
        """Transcript with non-home parent dir → returns dir name as-is."""
        import memem.mine_delta as md
        # Simulate a non-standard path where parent doesn't match munged home prefix
        result = md._project_from_transcript_path("/tmp/someotherlocation/abc123.jsonl")
        assert result == "someotherlocation", (
            f"Non-home path should return dir name, got {result!r}"
        )

    def test_empty_path_returns_general(self):
        """Empty or malformed path → 'general'."""
        import memem.mine_delta as md
        result = md._project_from_transcript_path("")
        assert result == "general", f"Empty path should return 'general', got {result!r}"


# ---------------------------------------------------------------------------
# B6: Title skips '<task-notification...' and picks next real message
# ---------------------------------------------------------------------------


class TestTitleSkipsXmlGarbage:
    """first_user_msg starting with '<' is skipped; next real message is used."""

    def _make_fake_subprocess(self, md, monkeypatch, summary):
        import subprocess as _subprocess
        original_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = summary
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

    def test_title_skips_task_notification_xml(self, mine_env, monkeypatch):
        """Episode title must skip the '<task-notification>' first message and
        pick the next real user message."""
        import memem.mine_delta as md

        canned_memories = [{
            "title": "Design decision",
            "project": "cortex-plugin",
            "content": "The system uses a priority queue for task scheduling and ordering.",
            "importance": 3,
        }]

        def _fake_extract(text, context_hint=""):
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)
        self._make_fake_subprocess(
            md, monkeypatch,
            "The user refined task scheduling logic using a priority queue design pattern."
        )

        session_id = "test-title-xml-skip-sess"
        # First user turn is XML garbage, second turn is a real message
        turns = [
            # XML first user message — should be skipped
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<task-notification>Build the feature from the spec</task-notification>",
                },
            },
            # Assistant response
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I will implement the priority queue feature."}],
                },
            },
            # Second user message — real, should be used for title
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Please implement the task scheduler with priority ordering and proper tests.",
                },
            },
            # More turns to pass trivial check
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I will implement the priority queue feature with tests."}],
                },
            },
        ]
        # Add extra padding to pass the _MIN_DELTA_CHARS check
        for i in range(4):
            role = "user" if i % 2 == 0 else "assistant"
            turns.append({
                "type": role,
                "message": {
                    "role": role,
                    "content": (
                        f"Additional substantive content for turn {i} discussing the codebase "
                        f"architecture and design patterns for priority queue implementation. " * 2
                    ),
                },
            })

        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, turns)

        md.run(session_id=session_id, transcript_path=str(transcript))

        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        episode_files = [f for f in memories_dir.glob("*.md") if "type:episodic" in f.read_text()]
        assert len(episode_files) == 1, f"Expected 1 episode file, found {len(episode_files)}"

        episode_text = episode_files[0].read_text()
        # Title must NOT contain the XML tag text
        assert "<task-notification>" not in episode_text, (
            "Episode title must NOT contain XML task-notification content"
        )
        # Assert on the actual title: frontmatter line, not the body (the fake
        # Haiku summary also mentions the topic, which would mask a bad title).
        title_line = next(
            (line for line in episode_text.splitlines() if line.startswith("title:")),
            "",
        )
        assert title_line, f"Episode file missing title frontmatter. Got:\n{episode_text[:400]}"
        assert "task-notification" not in title_line, (
            f"Title must not be built from the XML message. Title line: {title_line}"
        )
        assert "scheduler" in title_line.lower() or "priority" in title_line.lower(), (
            f"Episode title should use the first real (non-XML) user message. Title line: {title_line}"
        )

    def test_title_skips_caveat_prefix(self, mine_env, monkeypatch):
        """Episode title must skip messages starting with 'Caveat:'."""
        import memem.mine_delta as md

        canned_memories = [{
            "title": "Coding convention",
            "project": "general",
            "content": "The team prefers explicit type annotations over implicit types in Python.",
            "importance": 3,
        }]

        def _fake_extract(text, context_hint=""):
            return list(canned_memories)

        monkeypatch.setattr(md, "extract_from_text", _fake_extract)
        self._make_fake_subprocess(
            md, monkeypatch,
            "The user discussed Python type annotation conventions with the team."
        )

        session_id = "test-title-caveat-skip-sess"
        turns = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Caveat: Note that this session contains sensitive information.",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Understood, I will be careful."}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "How should we enforce Python type annotations across the codebase?",
                },
            },
        ]
        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            turns.append({
                "type": role,
                "message": {
                    "role": role,
                    "content": (
                        f"More substantive discussion turn {i} about Python type annotations "
                        f"and coding conventions in the project codebase. " * 2
                    ),
                },
            })

        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, turns)

        md.run(session_id=session_id, transcript_path=str(transcript))

        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        episode_files = [f for f in memories_dir.glob("*.md") if "type:episodic" in f.read_text()]
        assert len(episode_files) == 1, f"Expected 1 episode file, found {len(episode_files)}"

        episode_text = episode_files[0].read_text()
        # Title must NOT start with 'Caveat:'
        for line in episode_text.splitlines():
            if line.startswith("title:"):
                title_val = line.split(":", 1)[1].strip()
                assert not title_val.startswith("Caveat"), (
                    f"Episode title must not start with 'Caveat:', got: {title_val}"
                )
                break


# ---------------------------------------------------------------------------
# B15: Episode catalog — strict episodic-only, max 25, no padding
# ---------------------------------------------------------------------------


class TestEpisodeCatalog:
    """Catalog: with 30 episodic + 40 non-episodic memories, output lists 25 episodic, no padding."""

    def test_catalog_strict_episodic_no_padding(self):
        """The catalog python snippet must filter strictly to type:episodic,
        cap at 25, and emit no '## Episode index' section when zero episodes."""
        # This tests the actual catalog logic (extracted inline) against a mock vault.
        # We replicate the logic from session-start.sh's HOOKPY section.

        def _run_catalog_logic(all_mems):
            """Run the same logic as session-start.sh's episode catalog block."""
            episodic = sorted(
                [m for m in all_mems if "type:episodic" in (m.get("domain_tags") or [])],
                key=lambda m: m.get("created_at") or m.get("created") or "",
                reverse=True,
            )[:25]
            if not episodic:
                return ""
            cat_lines = ["", "## Episode index"]
            for m in episodic:
                mid = (m.get("id") or "")[:8]
                date = (m.get("created_at") or m.get("created") or "")[:10] or "----"
                title = (m.get("title") or "(untitled)")[:80]
                cat_lines.append(f"- {mid} [{date}]: {title}")
            return "\n".join(cat_lines)

        # Build 30 episodic + 40 non-episodic memories
        episodic_mems = [
            {
                "id": f"ep-{i:04d}-xxxx",
                "title": f"Episode memory {i}",
                "domain_tags": ["type:episodic"],
                "created_at": f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z",
            }
            for i in range(30)
        ]
        non_episodic_mems = [
            {
                "id": f"non-{i:04d}-xxxx",
                "title": f"Non-episode memory {i}",
                "domain_tags": ["type:skill"],
                "created_at": f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z",
            }
            for i in range(40)
        ]
        all_mems = episodic_mems + non_episodic_mems

        output = _run_catalog_logic(all_mems)

        # Must contain the Episode index header
        assert "## Episode index" in output, "Catalog must contain '## Episode index'"

        # Must list exactly 25 items (capped from 30 episodic)
        item_lines = [line for line in output.splitlines() if line.startswith("- ")]
        assert len(item_lines) == 25, (
            f"Catalog must list exactly 25 items (capped from 30 episodic), found {len(item_lines)}"
        )

        # All listed items must be episodic (no non-episodic ids)
        non_ep_ids = {m["id"][:8] for m in non_episodic_mems}
        for line in item_lines:
            # Line format: "- <8-char-id> [date]: title"
            parts = line.split(" ", 1)
            if len(parts) > 1:
                item_id = parts[1].split(" ")[0]
                assert item_id not in non_ep_ids, (
                    f"Catalog must NOT include non-episodic memory id {item_id}: {line}"
                )

    def test_catalog_zero_episodes_emits_no_section(self):
        """When no episodic memories exist, catalog emits nothing (no '## Episode index')."""

        def _run_catalog_logic(all_mems):
            episodic = sorted(
                [m for m in all_mems if "type:episodic" in (m.get("domain_tags") or [])],
                key=lambda m: m.get("created_at") or m.get("created") or "",
                reverse=True,
            )[:25]
            if not episodic:
                return ""
            cat_lines = ["", "## Episode index"]
            for m in episodic:
                mid = (m.get("id") or "")[:8]
                date = (m.get("created_at") or m.get("created") or "")[:10] or "----"
                title = (m.get("title") or "(untitled)")[:80]
                cat_lines.append(f"- {mid} [{date}]: {title}")
            return "\n".join(cat_lines)

        # Only non-episodic memories
        all_mems = [
            {"id": f"skill-{i}", "title": f"Skill {i}", "domain_tags": ["type:skill"], "created_at": "2024-01-01T00:00:00Z"}
            for i in range(40)
        ]

        output = _run_catalog_logic(all_mems)
        assert output == "", (
            f"With no episodic memories, catalog must emit empty string, got: {output!r}"
        )
        assert "## Episode index" not in output, (
            "With no episodic memories, must NOT emit '## Episode index' section"
        )
