"""Tests for PROFILE op reconciler integration in memem.mine_delta.

All tests use canned Haiku responses (no live claude calls). Tests verify:
- PROFILE user op → line lands in profile_user.md correct section + audit event
- PROFILE project op → profile_<project>.md correct section + audit event
- PROFILE op does NOT save a vault memory
- project=='general' + profile=='project' → degraded ADD
- Invalid section → degraded ADD (fact preserved)
- Duplicate line → reconcile_noop audit, no double-append
- >3 PROFILE ops → 4th degrades to ADD
- Mixed delta (ADD + PROFILE + NOOP) all execute correctly
- rejected_full (canned append_fact failure) → degraded ADD
- Unknown profile field value → degraded ADD
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirror test_reconcile.py patterns)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 6) -> list[dict]:
    """Synthetic turns with substantive content."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = (
            f"This is turn {i} with substantive content about codebase architecture "
            f"and important design decisions that must be remembered. "
            f"The user prefers dark mode and uses pytest for all tests. " * 2
        )
        turns.append({
            "type": role,
            "message": {"role": role, "content": text},
        })
    return turns


def _canned_reconcile_response(ops: list[dict]) -> str:
    return json.dumps(ops)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mine_env(tmp_path, monkeypatch):
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
    import memem.profiles as _profiles
    importlib.reload(_profiles)
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
    importlib.reload(_profiles)
    importlib.reload(_md)


def _make_subprocess_patcher(md, monkeypatch, reconcile_responses: list[str]):
    """Patch subprocess.run so reconcile calls return canned responses.

    Identified by _HAIKU_RECONCILE_SYSTEM in the cmd args (mirrors test_reconcile.py).
    Episode calls get a generic response.
    """
    import subprocess as _subprocess
    original_run = _subprocess.run

    reconcile_call_idx = [0]

    def _fake_run(cmd, **kwargs):
        if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
            idx = reconcile_call_idx[0]
            reconcile_call_idx[0] += 1
            response = reconcile_responses[idx % len(reconcile_responses)] if reconcile_responses else "[]"

            class FakeResult:
                returncode = 0
                stdout = response
                stderr = ""
            return FakeResult()

        if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
            class FakeEpisodeResult:
                returncode = 0
                stdout = "Canned episode summary."
                stderr = ""
            return FakeEpisodeResult()

        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    monkeypatch.setattr(md.subprocess, "run", _fake_run)
    return reconcile_call_idx


def _read_events(state_dir: Path) -> list[dict]:
    """Read all events from events.jsonl."""
    events_path = state_dir / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _events_by_op(state_dir: Path, op_name: str) -> list[dict]:
    """Return all events with op == op_name."""
    # Events are stored with key "op" (not "event")
    return [e for e in _read_events(state_dir) if e.get("op") == op_name]


# ---------------------------------------------------------------------------
# Test: PROFILE user op → fact in profile_user.md, no vault memory
# ---------------------------------------------------------------------------


class TestProfileUserOp:
    """PROFILE user op: fact appended to profile_user.md correct section."""

    def test_profile_user_op_writes_fact(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-user-op"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "User dark mode pref", "project": "general",
                   "content": "User strongly prefers dark mode in all editors.",
                   "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Preferences",
             "line": "Strongly prefers dark mode in all editors",
             "reason": "stable user preference"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Profile file should exist with the fact
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_file = profiles_dir / "profile_user.md"
        assert profile_file.exists(), f"profile_user.md should exist at {profile_file}"
        content = profile_file.read_text()
        assert "Strongly prefers dark mode" in content, (
            f"Fact not found in profile. Content:\n{content}"
        )
        assert "## Preferences" in content, "Preferences section should be present"

        # Should NOT be saved as a vault memory
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 0, (
            "PROFILE op should NOT create a vault memory. Found: "
            + str([f.name for f in non_episode_files])
        )

    def test_profile_user_op_emits_audit_event(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-user-audit"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "User pytest pref", "project": "general",
                   "content": "User always uses pytest for testing.", "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Conventions",
             "line": "Always uses pytest for testing",
             "reason": "stable convention"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        reconcile_profile_events = _events_by_op(mine_env["state_dir"], "reconcile_profile")
        assert len(reconcile_profile_events) >= 1, (
            f"Expected reconcile_profile audit event. Events: {_read_events(mine_env['state_dir'])}"
        )
        evt = reconcile_profile_events[0]
        assert evt.get("profile") == "user", f"Expected profile='user', got {evt.get('profile')}"
        assert evt.get("section") == "Conventions", f"Expected section='Conventions', got {evt.get('section')}"


# ---------------------------------------------------------------------------
# Test: PROFILE project op → profile_<project>.md
# ---------------------------------------------------------------------------


class TestProfileProjectOp:
    """PROFILE project op: fact appended to profile_<project>.md."""

    def test_profile_project_op_writes_fact(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-project-op"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Cortex plugin stack", "project": "cortex-plugin",
                   "content": "Project uses Python 3.11, structlog, pytest.",
                   "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "project", "section": "Stack & Structure",
             "line": "Python 3.11 with structlog and pytest",
             "reason": "project stack fact"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Profile file should exist — project name 'cortex-plugin' → profile_cortex-plugin.md
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_file = profiles_dir / "profile_cortex-plugin.md"
        assert profile_file.exists(), (
            f"profile_cortex-plugin.md should exist at {profile_file}. "
            f"Files: {list(profiles_dir.glob('*.md')) if profiles_dir.exists() else 'dir missing'}"
        )
        content = profile_file.read_text()
        assert "Python 3.11 with structlog and pytest" in content, (
            f"Fact not found in project profile. Content:\n{content}"
        )

        # NOT a vault memory
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 0, (
            "PROFILE op should NOT create a vault memory."
        )


# ---------------------------------------------------------------------------
# Test: project=='general' + profile=='project' → degraded ADD
# ---------------------------------------------------------------------------


class TestProfileProjectGeneralDegradedAdd:
    """project='general' + profile='project' → op degrades to ADD (no meaningful project scope)."""

    def test_general_project_profile_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-general-degrade"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Some project fact", "project": "general",
                   "content": "This is a project-level fact for an unspecified project.",
                   "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "project", "section": "Identity",
             "line": "This is a project-level fact for an unspecified project",
             "reason": "project identity"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Should fall back to ADD — fact preserved in vault
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            f"Degraded ADD should create a vault memory. Found: {[f.name for f in non_episode_files]}"
        )

        # No profile files should exist
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_files = list(profiles_dir.glob("profile_*.md")) if profiles_dir.exists() else []
        assert len(profile_files) == 0, (
            f"No profile file should be created for general project. Found: {profile_files}"
        )


# ---------------------------------------------------------------------------
# Test: Invalid section → degraded ADD (fact preserved)
# ---------------------------------------------------------------------------


class TestProfileInvalidSectionDegradedAdd:
    """Invalid section → degraded ADD (unknown-section-degraded audit reason)."""

    def test_invalid_section_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-invalid-section"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "User habit", "project": "general",
                   "content": "User always commits before running migrations.",
                   "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "BogusSection",
             "line": "Always commits before running migrations",
             "reason": "user habit"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Fact should be preserved as a vault ADD
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            f"Invalid section → degraded ADD should save fact to vault. "
            f"Found: {[f.name for f in non_episode_files]}"
        )

        # Audit event should show unknown-section-degraded reason
        add_events = _events_by_op(mine_env["state_dir"], "reconcile_add")
        assert len(add_events) >= 1, (
            f"Expected reconcile_add event. Events: {_read_events(mine_env['state_dir'])}"
        )
        degraded = [e for e in add_events if "unknown-section-degraded" in (e.get("reason") or "")]
        assert len(degraded) >= 1, (
            f"Expected unknown-section-degraded reason in reconcile_add. Events: {add_events}"
        )


# ---------------------------------------------------------------------------
# Test: Duplicate line → reconcile_noop, no double-append
# ---------------------------------------------------------------------------


class TestProfileDuplicateLine:
    """Duplicate fact → reconcile_noop audit, no double-append."""

    def test_duplicate_profile_line_emits_noop(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.profiles as profiles
        importlib.reload(profiles)

        # Pre-insert the fact so the second write will be a duplicate
        profiles.append_fact("user", "Preferences", "Always uses dark mode")

        session_id = "test-profile-duplicate"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Dark mode pref", "project": "general",
                   "content": "User always uses dark mode.", "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        # Same line → append_fact returns 'duplicate'
        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Preferences",
             "line": "Always uses dark mode",
             "reason": "user pref"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Profile file should have exactly one bullet (no double-append)
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_file = profiles_dir / "profile_user.md"
        assert profile_file.exists(), "profile_user.md should exist"
        content = profile_file.read_text()
        occurrences = content.count("Always uses dark mode")
        assert occurrences == 1, (
            f"Fact should appear exactly once in profile, found {occurrences}.\n{content}"
        )

        # reconcile_noop event should be emitted for the duplicate
        noop_events = _events_by_op(mine_env["state_dir"], "reconcile_noop")
        profile_dupes = [e for e in noop_events if e.get("reason") == "profile-duplicate"]
        assert len(profile_dupes) >= 1, (
            f"Expected reconcile_noop with reason='profile-duplicate'. "
            f"Events: {_read_events(mine_env['state_dir'])}"
        )

        # No vault memory should be saved
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 0, (
            "Duplicate PROFILE op should NOT create a vault memory."
        )


# ---------------------------------------------------------------------------
# Test: >3 PROFILE ops → 4th degrades to ADD
# ---------------------------------------------------------------------------


class TestProfileCapEnforcement:
    """Cap: >3 PROFILE ops per delta → excess ops degrade to ADD."""

    def test_fourth_profile_op_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-cap"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        # 4 genuinely distinct preference facts (must pass rapidfuzz dedup threshold)
        fact_lines = [
            "Always uses vim keybindings in all editors",
            "Prefers Python over JavaScript for backend services",
            "Runs all tests before committing any code changes",
            "Uses black formatter with 88 char line length",
        ]
        canned = [
            {"title": f"Pref {i}", "project": "general",
             "content": fact_lines[i],
             "importance": 4}
            for i in range(4)
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        ops = [
            {"index": i, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Preferences",
             "line": fact_lines[i],
             "reason": "user pref"}
            for i in range(4)
        ]
        reconcile_resp = _canned_reconcile_response(ops)
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # First 3 should be in profile
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_file = profiles_dir / "profile_user.md"
        assert profile_file.exists(), "profile_user.md should exist"
        profile_content = profile_file.read_text()
        for i in range(3):
            assert fact_lines[i] in profile_content, (
                f"Fact {i} should be in profile. Content:\n{profile_content}"
            )

        # 4th should be saved as a vault memory (degraded ADD)
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            "4th PROFILE op should degrade to ADD (vault memory). "
            f"Found: {[f.name for f in non_episode_files]}"
        )
        # The vault memory should contain the 4th fact
        fact3_found = any(
            fact_lines[3] in f.read_text()
            for f in non_episode_files
        )
        assert fact3_found, (
            "4th fact should be in a vault memory. Files:\n"
            + "\n".join(f.read_text()[:200] for f in non_episode_files)
        )


# ---------------------------------------------------------------------------
# Test: Mixed delta (ADD + PROFILE + NOOP) all execute correctly
# ---------------------------------------------------------------------------


class TestMixedDelta:
    """Mixed delta: ADD + PROFILE + NOOP all execute correctly."""

    def test_mixed_delta_executes_all_ops(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-mixed-delta"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [
            {"title": "Normal memory", "project": "cortex-plugin",
             "content": "The project uses Python 3.11 and structlog.", "importance": 3},
            {"title": "User env fact", "project": "general",
             "content": "User runs Ubuntu 22.04 on all machines.", "importance": 4},
            {"title": "Redundant fact", "project": "general",
             "content": "Python uses indentation for scope.", "importance": 1},
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None,
             "profile": None, "section": None, "line": None,
             "reason": "new project fact"},
            {"index": 1, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Environment",
             "line": "Runs Ubuntu 22.04 on all machines",
             "reason": "stable env fact"},
            {"index": 2, "op": "NOOP", "target": "aabbccdd", "content": None,
             "profile": None, "section": None, "line": None,
             "reason": "already known"},
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # ADD → vault memory
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 1, (
            f"Only the ADD should create a vault memory. Found: {[f.name for f in non_episode_files]}"
        )

        # PROFILE → profile_user.md Environment section
        profiles_dir = mine_env["vault_dir"] / "memem" / "profiles"
        profile_file = profiles_dir / "profile_user.md"
        assert profile_file.exists(), "profile_user.md should exist"
        profile_content = profile_file.read_text()
        assert "Runs Ubuntu 22.04 on all machines" in profile_content, (
            f"Profile fact not found. Content:\n{profile_content}"
        )
        assert "## Environment" in profile_content

        # NOOP → no extra vault memory
        assert len(non_episode_files) == 1, "NOOP should not create any memory"


# ---------------------------------------------------------------------------
# Test: rejected_full → degraded ADD (compaction failure monkeypatched)
# ---------------------------------------------------------------------------


class TestProfileRejectedFullDegradedAdd:
    """append_fact returns 'rejected_full' → degraded ADD, fact preserved."""

    def test_rejected_full_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.profiles as profiles
        importlib.reload(profiles)

        # Monkeypatch append_fact to return 'rejected_full'
        monkeypatch.setattr(profiles, "append_fact", lambda name, section, line: "rejected_full")

        session_id = "test-profile-rejected-full"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "User pref X", "project": "general",
                   "content": "User prefers X configuration style.", "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "user", "section": "Preferences",
             "line": "Prefers X configuration style",
             "reason": "user pref"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Fact should be saved as a vault ADD (degraded from PROFILE)
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            f"rejected_full → degraded ADD should create vault memory. "
            f"Found: {[f.name for f in non_episode_files]}"
        )


# ---------------------------------------------------------------------------
# Test: Unknown profile field value → degraded ADD
# ---------------------------------------------------------------------------


class TestProfileUnknownField:
    """Unknown profile field value → degraded ADD."""

    def test_unknown_profile_field_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-profile-unknown-field"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Some fact", "project": "general",
                   "content": "This fact would go to an unknown profile type.",
                   "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "PROFILE", "target": None, "content": None,
             "profile": "INVALID_PROFILE_TYPE", "section": "Preferences",
             "line": "This fact would go to an unknown profile type",
             "reason": "bad profile field"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Fact should be saved as a vault ADD
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            f"Unknown profile field → degraded ADD should create vault memory. "
            f"Found: {[f.name for f in non_episode_files]}"
        )
