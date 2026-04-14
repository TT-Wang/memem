"""Regression tests for v0.10.2 code-review fixes.

Each test targets a specific bug found in post-v0.10.0 review. The bug
description is in the docstring and in the relevant CHANGELOG entry.
"""

import importlib
import time


def test_get_installed_at_does_not_lazy_create(tmp_cortex_dir):
    """v0.10.2 fix #1: _get_installed_at must NOT recreate the marker on read.

    The v0.10.0 bug: cli.py --mine-all would clear the install-gate marker,
    then immediately call mine_all() → find_settled_sessions() →
    _get_installed_at(), which would lazy-create the marker with the current
    timestamp. The subsequent filter (st_mtime < installed_at) then rejected
    every historical session, making --mine-all a no-op.
    """
    from memem import session_state
    importlib.reload(session_state)

    # Ensure no marker exists
    session_state.clear_installed_at()
    assert not session_state.INSTALLED_AT_FILE.exists()

    # Read should return 0 and leave the filesystem alone
    assert session_state._get_installed_at() == 0.0
    assert not session_state.INSTALLED_AT_FILE.exists(), (
        "_get_installed_at must not lazy-create the marker on read"
    )


def test_ensure_installed_at_creates_marker(tmp_cortex_dir):
    """The new _ensure_installed_at is the ONLY path that creates the marker."""
    from memem import session_state
    importlib.reload(session_state)

    session_state.clear_installed_at()
    assert not session_state.INSTALLED_AT_FILE.exists()

    ts = session_state._ensure_installed_at()
    assert session_state.INSTALLED_AT_FILE.exists()
    assert abs(ts - time.time()) < 5.0  # within 5s of now


def test_find_settled_sessions_bypass_gate_param(tmp_cortex_dir, tmp_path, monkeypatch):
    """v0.10.2 fix #1: find_settled_sessions(bypass_gate=True) ignores the gate.

    When --mine-all passes bypass_gate=True, even a present install marker
    should be ignored so historical sessions are mined.
    """
    from memem import session_state
    importlib.reload(session_state)

    # Create a gate marker set to NOW — would normally filter out all historical
    session_state._ensure_installed_at()

    # Build a fake sessions dir with an old session file
    fake_sessions = tmp_path / "claude-projects"
    fake_sessions.mkdir()
    old_session = fake_sessions / "old.jsonl"
    old_session.write_text("x" * 10000)  # above 5000-byte threshold
    # Backdate it by 30 minutes so it's past settle gate AND pre-install
    old_time = time.time() - 1800
    import os
    os.utime(old_session, (old_time, old_time))

    monkeypatch.setattr(session_state, "SESSIONS_DIRS", [fake_sessions])

    # Without bypass: filtered out by install gate
    results = session_state.find_settled_sessions(bypass_gate=False)
    assert old_session not in results

    # With bypass: included
    results_bypass = session_state.find_settled_sessions(bypass_gate=True)
    assert old_session in results_bypass


def test_smart_recall_respects_scope_id(tmp_vault, monkeypatch):
    """v0.10.2 fix #5: smart_recall must filter by scope_id in the Haiku path.

    Previously smart_recall only honored scope_id on the fallback branches.
    When Haiku picked memories from the global index, they were loaded and
    returned without any scope filter, leaking cross-project memories.
    """
    from memem import recall
    importlib.reload(recall)

    fake_memories = [
        {"id": "aaaa1111", "title": "A1", "project": "projectA", "body": "from A", "essence": "from A"},
        {"id": "bbbb2222", "title": "B1", "project": "projectB", "body": "from B", "essence": "from B"},
    ]

    # Stub the loader and the memory_recall fallback
    monkeypatch.setattr(recall, "_load_obsidian_memories", lambda ids: list(fake_memories))
    monkeypatch.setattr(recall, "memory_recall", lambda prompt, scope_id="default", limit=10: "FALLBACK")

    # Stub INDEX_PATH directly on the recall module (bypasses the import-time binding)
    class FakeIndex:
        @staticmethod
        def exists():
            return True

        @staticmethod
        def read_text():
            return "(aaaa1111) A1\n(bbbb2222) B1\n"
    monkeypatch.setattr(recall, "INDEX_PATH", FakeIndex)

    # Make the capabilities check think Claude is available
    import memem.capabilities
    monkeypatch.setattr(memem.capabilities, "assembly_available", lambda: True)

    # Fake Haiku subprocess returning both IDs
    class FakeResult:
        returncode = 0
        stdout = "aaaa1111\nbbbb2222\n"
        stderr = ""
    monkeypatch.setattr(recall.subprocess, "run", lambda *a, **kw: FakeResult())

    # Scoped call: only projectA memory should survive
    output = recall.smart_recall("test", scope_id="projectA")
    assert "A1" in output, f"projectA memory missing from output: {output!r}"
    assert "B1" not in output, f"smart_recall leaked projectB memory: {output!r}"


def test_context_assemble_returns_materials_on_haiku_failure(tmp_vault, monkeypatch):
    """v0.10.2 fix #4: context_assemble must return raw materials on failure.

    Previously the exception branches returned `playbook_content or ""`,
    throwing away already-collected memories and transcript results when
    Haiku was unstable.
    """
    from memem import assembly
    importlib.reload(assembly)

    # context_assemble calls _search_memories (lazy imported from recall)
    # for its memory set. Patch that directly.
    from memem import recall
    monkeypatch.setattr(
        recall, "_search_memories",
        lambda query, scope_id=None, limit=20, record_access=False, expand_links=True: [
            {"id": "m1", "title": "Memory 1", "essence": "important context", "project": "test"},
        ],
    )
    monkeypatch.setattr(
        "memem.transcripts.transcript_search",
        lambda query, limit=3: "session log snippet",
    )
    import memem.capabilities
    monkeypatch.setattr(memem.capabilities, "assembly_available", lambda: True)

    # Haiku subprocess raises
    def boom(*args, **kwargs):
        raise RuntimeError("haiku down")
    monkeypatch.setattr(assembly.subprocess, "run", boom)

    result = assembly.context_assemble("what do we know about X?", project="default")

    # Must include the memory (not just playbook or transcript)
    assert "Memory 1" in result, (
        f"context_assemble dropped memories on Haiku failure. Got: {result!r}"
    )


def test_auto_recall_hook_uses_tempfile_for_large_message(tmp_path):
    """v0.10.2 fix #6: auto-recall.sh must read message from tempfile, not argv.

    A 1 MB+ user message would hit ARG_MAX on the previous argv-based design.
    This test validates the hook at least doesn't crash with a large input.
    We run the hook as a subprocess with a large message piped via stdin.
    """
    import json
    import subprocess
    from pathlib import Path

    hook_path = Path(__file__).resolve().parent.parent / "hooks" / "auto-recall.sh"

    # Craft a very large message (~500 KB) — enough to break argv on some systems
    big_message = "x " * 250_000
    hook_input = json.dumps({
        "session_id": "test-session",
        "message": big_message,
    })

    # Run the hook WITHOUT CLAUDE_PLUGIN_ROOT so it emits empty context quickly
    # (we're just testing that it doesn't crash on large input)
    result = subprocess.run(
        ["bash", str(hook_path)],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path), "MEMEM_DIR": str(tmp_path / ".memem"), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, f"hook crashed on large input: {result.stderr}"
    # Must emit valid JSON
    data = json.loads(result.stdout)
    assert "hookSpecificOutput" in data
