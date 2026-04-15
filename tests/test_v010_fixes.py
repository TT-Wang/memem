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


def test_find_settled_sessions_skips_root_project(tmp_cortex_dir, tmp_path, monkeypatch):
    """v0.11.x fix: sessions under `.claude/projects/-root/` must be
    filtered out. That directory is where headless `claude -p` subprocess
    invocations land — memem's own Haiku mining subprocesses end up there,
    and without this filter the miner picks them up as real sessions and
    tries to mine them (recursively mining mining).

    Found 2026-04-15 during the tail-bug heal run: 26% of "successfully
    mined" sessions were actually self-referential subprocess artifacts
    under `-root`, polluting ~10% of the memory vault.
    """
    from memem import session_state
    importlib.reload(session_state)

    # Build two fake project directories: one named `-root` (should be
    # filtered out) and one normal (should be returned).
    projects = tmp_path / "projects"
    (projects / "-root").mkdir(parents=True)
    (projects / "normal-project").mkdir(parents=True)

    # Both sessions are big enough and old enough to pass the other gates.
    old_time = time.time() - 1800
    root_session = projects / "-root" / "subprocess-artifact.jsonl"
    root_session.write_text("x" * 10000)
    import os as _os
    _os.utime(root_session, (old_time, old_time))

    real_session = projects / "normal-project" / "real-work.jsonl"
    real_session.write_text("y" * 10000)
    _os.utime(real_session, (old_time, old_time))

    monkeypatch.setattr(session_state, "SESSIONS_DIRS", [projects])

    results = session_state.find_settled_sessions(bypass_gate=True)

    assert real_session in results, (
        "normal project session should be returned"
    )
    assert root_session not in results, (
        "sessions under -root/ must be filtered out as mining subprocess artifacts"
    )


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


# ─── v0.11.x: mine_all error handling (split Fatal vs Transient) ─────


def test_mine_all_logs_transient_and_continues(
    tmp_cortex_dir, tmp_path, monkeypatch, caplog
):
    """Transient mining errors must be logged (not swallowed) and must not
    abort the mine_all loop. Each transient failure should be captured in
    the returned ``failures`` list with fatal=False.
    """
    import logging

    from memem import mining, session_state
    importlib.reload(session_state)
    importlib.reload(mining)

    # Build two fake sessions on disk — big enough to clear the size gate,
    # old enough to clear the settle gate.
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    now = time.time()
    fake_sessions = []
    for name in ("session-aaa", "session-bbb"):
        p = sessions_dir / f"{name}.jsonl"
        p.write_text("x" * 6000)
        old = now - 10_000
        import os as _os
        _os.utime(p, (old, old))
        fake_sessions.append(p)

    monkeypatch.setattr(session_state, "SESSIONS_DIRS", [tmp_path / "projects"])
    # Re-export from mining namespace so mine_all sees the patched list.
    monkeypatch.setattr(mining, "find_settled_sessions", session_state.find_settled_sessions)

    def fake_mine(jsonl_path):
        raise mining.TransientMiningError(f"fake transient: {jsonl_path}")

    monkeypatch.setattr(mining, "mine_session", fake_mine)

    with caplog.at_level(logging.WARNING, logger=mining.log.name):
        result = mining.mine_all(bypass_gate=True)

    # Both sessions seen, both counted as failed, nothing mined
    assert result["total_sessions"] == 2
    assert result["failed_sessions"] == 2
    assert result["newly_mined"] == 0
    # failures list must contain one entry per session with fatal=False
    assert len(result["failures"]) == 2
    for f in result["failures"]:
        assert f["fatal"] is False
        assert "fake transient" in f["error"]
    # Each failure must produce a warning log (not silent)
    transient_warnings = [
        rec for rec in caplog.records
        if "Transient mining failure" in rec.getMessage()
    ]
    assert len(transient_warnings) == 2, (
        f"expected 2 transient warnings, got {len(transient_warnings)}: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_mine_all_aborts_on_fatal(
    tmp_cortex_dir, tmp_path, monkeypatch, caplog
):
    """FatalMiningError must abort mine_all immediately and re-raise,
    so the CLI handler in cli.py can propagate FATAL_EXIT_CODE instead of
    letting mine-cron.sh relaunch into the same broken storage state.
    """
    import logging

    from memem import mining, session_state
    importlib.reload(session_state)
    importlib.reload(mining)

    # Build three fake sessions; the first should raise Fatal and halt.
    sessions_dir = tmp_path / "projects" / "demo"
    sessions_dir.mkdir(parents=True)
    now = time.time()
    import os as _os
    for name in ("a", "b", "c"):
        p = sessions_dir / f"session-{name}.jsonl"
        p.write_text("x" * 6000)
        old = now - 10_000 - ord(name)  # deterministic order
        _os.utime(p, (old, old))

    monkeypatch.setattr(session_state, "SESSIONS_DIRS", [tmp_path / "projects"])
    monkeypatch.setattr(mining, "find_settled_sessions", session_state.find_settled_sessions)

    call_count = {"n": 0}

    def fake_mine(jsonl_path):
        call_count["n"] += 1
        raise mining.FatalMiningError("disk full")

    monkeypatch.setattr(mining, "mine_session", fake_mine)

    with caplog.at_level(logging.ERROR, logger=mining.log.name):
        import pytest
        with pytest.raises(mining.FatalMiningError, match="disk full"):
            mining.mine_all(bypass_gate=True)

    # Exactly one call — the loop must have aborted on the first fatal.
    assert call_count["n"] == 1, (
        f"mine_all should abort on first FatalMiningError, but called "
        f"mine_session {call_count['n']} times"
    )
    # And it must have logged an "aborting run" message.
    assert any(
        "aborting run" in rec.getMessage()
        for rec in caplog.records
    ), "expected an 'aborting run' error log line"


# ─── v0.11.x: sliding-window chunked mining (tail bug fix) ─────────────


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess returned by subprocess.run."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_chunked_mining_small_session_fast_path(tmp_cortex_dir, monkeypatch):
    """A small session (< _MAX_PROMPT_CHARS) must hit the fast path:
    exactly one subprocess.run call to Haiku, no chunking overhead.
    """
    from memem import mining
    importlib.reload(mining)

    calls = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        calls.append(input or "")
        return _FakeCompleted(
            returncode=0,
            stdout='[{"title": "Small session insight", "content": "x", "project": "general", "importance": 3}]',
        )

    monkeypatch.setattr(mining.subprocess, "run", fake_run)

    # ~2k chars total — well under the 150k budget
    messages = ["User: hello there", "Assistant: hi back"] * 50
    total = sum(len(m) + 2 for m in messages)
    assert total < mining._MAX_PROMPT_CHARS

    insights = mining._summarize_session_haiku(messages)

    assert len(calls) == 1, (
        f"small session must hit fast path with exactly 1 Haiku call, got {len(calls)}"
    )
    assert len(insights) == 1
    assert insights[0]["title"] == "Small session insight"


def test_chunked_mining_large_session_splits_chunks(tmp_cortex_dir, monkeypatch, caplog):
    """A session larger than _MAX_PROMPT_CHARS must be split into multiple
    chunks, each sent to Haiku, and the insights aggregated across chunks.
    """
    import logging

    from memem import mining
    importlib.reload(mining)

    # Build ~500k chars of fake messages (well over the 150k budget).
    # Each message is 50k chars → 10 messages → 500k + separator overhead.
    messages = [f"User: {'x' * 50_000}" for _ in range(10)]
    total = sum(len(m) + 2 for m in messages)
    assert total > mining._MAX_PROMPT_CHARS, f"test precondition failed: {total}"

    # Each Haiku call returns one insight — counter so we can tell them apart.
    call_inputs: list[str] = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        call_inputs.append(input or "")
        idx = len(call_inputs)
        return _FakeCompleted(
            returncode=0,
            stdout=(
                f'[{{"title": "Chunk {idx} insight", '
                f'"content": "content from chunk {idx}", '
                f'"project": "general", "importance": 3}}]'
            ),
        )

    monkeypatch.setattr(mining.subprocess, "run", fake_run)

    with caplog.at_level(logging.INFO, logger=mining.log.name):
        insights = mining._summarize_session_haiku(messages)

    # Must have made more than one Haiku call (chunked path).
    assert len(call_inputs) > 1, (
        f"large session should be chunked, but only {len(call_inputs)} Haiku calls were made"
    )
    # And must have aggregated one insight per chunk.
    assert len(insights) == len(call_inputs), (
        f"expected {len(call_inputs)} aggregated insights, got {len(insights)}"
    )
    # Chunk-level progress must be logged.
    chunk_logs = [
        rec for rec in caplog.records if "Mining chunk" in rec.getMessage()
    ]
    assert len(chunk_logs) == len(call_inputs), (
        f"expected {len(call_inputs)} chunk-progress log lines, got {len(chunk_logs)}"
    )


def test_chunked_mining_any_chunk_failure_fails_whole_session(
    tmp_cortex_dir, monkeypatch
):
    """Pessimistic failure semantics: if ANY chunk's Haiku call fails
    with TransientMiningError, the whole session must abort with that
    error so the daemon marks STATUS_FAILED and retries from chunk 1.
    """
    from memem import mining
    importlib.reload(mining)

    # Same ~500k char setup as the previous test.
    messages = [f"User: {'x' * 50_000}" for _ in range(10)]
    assert sum(len(m) + 2 for m in messages) > mining._MAX_PROMPT_CHARS

    call_count = {"n": 0}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First chunk succeeds
            return _FakeCompleted(
                returncode=0,
                stdout='[{"title": "ok", "content": "c", "project": "general", "importance": 3}]',
            )
        # Second chunk's Haiku call fails — simulated rate-limit / network error
        return _FakeCompleted(returncode=1, stderr="simulated haiku failure")

    monkeypatch.setattr(mining.subprocess, "run", fake_run)

    import pytest
    with pytest.raises(mining.TransientMiningError, match="simulated haiku failure"):
        mining._summarize_session_haiku(messages)

    # Sanity: the loop must have reached chunk 2 (where the failure lives)
    # but not continued past it. We can't know the exact chunk boundaries
    # without re-running the splitter, but we can at least confirm that
    # the second Haiku call was reached.
    assert call_count["n"] >= 2, (
        f"expected at least 2 Haiku calls before the failure, got {call_count['n']}"
    )
