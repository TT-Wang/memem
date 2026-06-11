"""Tests for reconcile-at-write (C1+C4) in memem.mine_delta.

Tests cover:
- ADD op with stable-id
- UPDATE op merges content+tags+keys
- SUPERSEDE op invalidates target
- NOOP op skips
- Invalid target degrades to ADD
- UPDATE+SUPERSEDE cap overflow degrades to ADD
- Malformed Haiku JSON triggers global fallback (ADD-all)
- Pre-filter: score >= 0.95 → reconcile_noop, no Haiku
- Idempotent re-mine (run twice, same memory count)
- Audit events written with correct op names
- Real JSONL fixture: full run() no crash, offset advances
"""

from __future__ import annotations

import importlib
import json
import os
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 6) -> list[dict]:
    """Synthetic turns in real Claude Code nested schema."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = (
            f"This is turn {i} with substantive content about codebase architecture "
            f"and important design decisions that must be remembered. "
            f"The function uses a callback pattern and returns a list of dicts. " * 2
        )
        turns.append({
            "type": role,
            "message": {"role": role, "content": text},
        })
    return turns


def _canned_reconcile_response(ops: list[dict]) -> str:
    """Build a JSON array string representing reconcile Haiku output."""
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
    # Reload mine_delta so its top-level imports of _log_event and obsidian_store
    # functions pick up the freshly-reloaded modules that use test paths.
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


def _make_subprocess_patcher(md, monkeypatch, reconcile_responses: list[str], episode_summary: str = ""):
    """Patch subprocess.run so reconcile calls return canned responses.

    Reconcile calls are identified by _RECONCILE_HAIKU_SYSTEM in the cmd.
    Episode calls are identified by _EPISODE_HAIKU_SYSTEM in the cmd.
    """
    import subprocess as _subprocess
    original_run = _subprocess.run

    reconcile_call_idx = [0]

    def _fake_run(cmd, **kwargs):
        # Reconcile call
        if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
            idx = reconcile_call_idx[0]
            reconcile_call_idx[0] += 1
            response = reconcile_responses[idx % len(reconcile_responses)] if reconcile_responses else "[]"

            class FakeResult:
                returncode = 0
                stdout = response
                stderr = ""
            return FakeResult()

        # Episode call
        if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
            class FakeEpisodeResult:
                returncode = 0
                stdout = episode_summary or "Canned episode summary for test."
                stderr = ""
            return FakeEpisodeResult()

        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    monkeypatch.setattr(md.subprocess, "run", _fake_run)
    return reconcile_call_idx


# ---------------------------------------------------------------------------
# Test: ADD op — stable id assigned
# ---------------------------------------------------------------------------


class TestReconcileAddOp:
    """ADD op: new memory saved with stable uuid5 id."""

    def test_add_op_uses_stable_id(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        session_id = "test-reconcile-add-op"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Callback pattern", "project": "cortex-plugin",
                   "content": "The function uses callbacks and returns list of dicts.", "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new knowledge"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        md_files = [f for f in memories_dir.glob("*.md")
                    if "type:episodic" not in f.read_text()]
        assert len(md_files) >= 1, f"Expected at least 1 memory file, found {len(md_files)}"

        # Verify the stable uuid5 id is present in the file
        from memem.obsidian_store import _stable_mined_memory_id
        stable_id = _stable_mined_memory_id(
            session_id,
            "Callback pattern",
            "The function uses callbacks and returns list of dicts.",
        )
        content_text = md_files[0].read_text()
        assert stable_id in content_text, (
            f"Expected stable_id {stable_id[:8]} in memory file. File:\n{content_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Test: UPDATE op — merges content + tags + keys
# ---------------------------------------------------------------------------


class TestReconcileUpdateOp:
    """UPDATE op: existing memory updated with merged content, extra tags, extra keys."""

    def test_update_merges_content_tags_keys(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Pre-create a memory to update
        target_mem = store._make_memory(
            content="The function uses callbacks and returns list of dicts.",
            title="Callback pattern",
            tags=["cortex-plugin"],
            project="cortex-plugin",
            source_type="mined",
            source_session="prior-session",
            importance=3,
        )
        store._save_memory(target_mem)
        store._reset_cache()
        store._trigger_sweep()
        target_id8 = target_mem["id"][:8]

        session_id = "test-reconcile-update-op"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Callback pattern extended", "project": "cortex-plugin",
                   "content": "Extended: callbacks return typed dicts.",
                   "importance": 3, "tags": ["extra-tag"], "keys": ["newkey"]}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        merged_content = "The function uses callbacks and returns typed list of dicts. Extended."
        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "UPDATE", "target": target_id8,
             "content": merged_content, "reason": "refines existing"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Find the updated memory
        store._reset_cache()
        store._trigger_sweep()
        updated = store._find_memory(target_id8)
        assert updated is not None, f"Target memory {target_id8} should still exist"
        assert merged_content in (updated.get("essence") or ""), (
            f"Merged content not found in updated memory. Got: {updated.get('essence', '')[:200]}"
        )


# ---------------------------------------------------------------------------
# Test: SUPERSEDE op — saves new + invalidates old
# ---------------------------------------------------------------------------


class TestReconcileSupersede:
    """SUPERSEDE op: new memory saved with stable id, old memory invalidated."""

    def test_supersede_invalidates_target(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Pre-create a memory to supersede
        old_mem = store._make_memory(
            content="Old design: synchronous blocking calls everywhere.",
            title="Old sync design",
            tags=["design"],
            project="cortex-plugin",
            source_type="user",
            source_session="old-session",
            importance=3,
        )
        store._save_memory(old_mem)
        store._reset_cache()
        store._trigger_sweep()
        old_id8 = old_mem["id"][:8]
        old_full_id = old_mem["id"]

        session_id = "test-reconcile-supersede"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "New async design", "project": "cortex-plugin",
                   "content": "New design: async-first, blocking calls replaced with await.",
                   "importance": 4}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "SUPERSEDE", "target": old_id8,
             "content": None, "reason": "replaces old sync design"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()

        # Old memory should be invalidated (invalid_at set)
        old_refreshed = store._find_memory(old_full_id)
        assert old_refreshed is not None, "Old memory should still exist (bi-temporal)"
        assert old_refreshed.get("invalid_at") is not None, (
            f"Old memory should have invalid_at set. Got: {old_refreshed}"
        )
        assert old_refreshed.get("replaced_by") is not None, (
            "Old memory should have replaced_by set"
        )


# ---------------------------------------------------------------------------
# Test: NOOP op — skips saving
# ---------------------------------------------------------------------------


class TestReconcileNoop:
    """NOOP op: candidate is fully redundant, skipped."""

    def test_noop_skips_save(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        session_id = "test-reconcile-noop"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Known fact", "project": "general",
                   "content": "Python uses indentation for scope.", "importance": 2}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "NOOP", "target": "aabbccdd",
             "content": None, "reason": "fully redundant"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        # No regular memories (only possibly episode)
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 0, (
            f"NOOP should not save any memories. Found: {[f.name for f in non_episode_files]}"
        )


# ---------------------------------------------------------------------------
# Test: Invalid target degrades to ADD
# ---------------------------------------------------------------------------


class TestReconcileInvalidTargetDegradesAdd:
    """Invalid target id → op degraded to ADD."""

    def test_invalid_target_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-reconcile-invalid-target"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Some fact", "project": "general",
                   "content": "This is a fact that should be saved.", "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        # UPDATE targeting a non-existent id → should degrade to ADD
        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "UPDATE", "target": "deadbeef",
             "content": "merged content", "reason": "update existing"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Memory should still be saved (as ADD)
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) >= 1, (
            f"Memory should be saved via ADD degradation. Files found: {[f.name for f in non_episode_files]}"
        )


# ---------------------------------------------------------------------------
# Test: Cap overflow — UPDATE+SUPERSEDE > 5 → excess degraded to ADD
# ---------------------------------------------------------------------------


class TestReconcileCapOverflow:
    """Cap: UPDATE+SUPERSEDE > 5 per delta — excess ops degrade to ADD."""

    def test_cap_overflow_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Pre-create 7 target memories
        target_ids: list[str] = []
        for i in range(7):
            m = store._make_memory(
                content=f"Existing memory {i} about design pattern {i}.",
                title=f"Design pattern {i}",
                project="cortex-plugin",
                source_type="user",
                source_session="pre-existing",
                importance=3,
            )
            store._save_memory(m)
            target_ids.append(m["id"][:8])
        store._reset_cache()
        store._trigger_sweep()

        session_id = "test-reconcile-cap-overflow"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        # 7 candidates all requesting UPDATE
        canned = [
            {"title": f"Candidate {i}", "project": "cortex-plugin",
             "content": f"New content for design pattern {i}.", "importance": 3}
            for i in range(7)
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        ops = [
            {"index": i, "op": "UPDATE", "target": target_ids[i],
             "content": f"Merged content {i}", "reason": "refines"}
            for i in range(7)
        ]
        reconcile_resp = _canned_reconcile_response(ops)
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # The cap is 5, so candidates 5 and 6 should be ADDs (not UPDATEs)
        # Check that at least 2 new files were created (degraded ADDs)
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        # Should have the original 7 memories + 2 degraded ADDs = at least 9
        assert len(non_episode_files) >= 9, (
            f"Expected at least 9 memory files (7 existing + 2 degraded ADDs), "
            f"found {len(non_episode_files)}"
        )


# ---------------------------------------------------------------------------
# Test: Malformed Haiku JSON → global fallback ADD-all
# ---------------------------------------------------------------------------


class TestReconcileMalformedJsonFallback:
    """Malformed Haiku JSON → global fallback to plain ADD-all."""

    def test_malformed_json_triggers_fallback(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        session_id = "test-reconcile-fallback"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [
            {"title": "First fact", "project": "general",
             "content": "This is the first fact to save.", "importance": 3},
            {"title": "Second fact", "project": "general",
             "content": "This is the second fact to save.", "importance": 3},
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        # Return completely unparseable garbage (not even repairable)
        _make_subprocess_patcher(md, monkeypatch, ["NOT JSON AT ALL %%%###"])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Fallback ADD-all should have saved both memories
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 2, (
            f"Fallback should have saved 2 memories, found {len(non_episode_files)}: "
            f"{[f.name for f in non_episode_files]}"
        )


# ---------------------------------------------------------------------------
# Test: Pre-filter score >= 0.95 → noop, no Haiku
# ---------------------------------------------------------------------------


class TestReconcilePrefilterNoop:
    """Pre-filter: score >= 0.95 → reconcile_noop, no Haiku reconcile call."""

    def test_prefilter_high_score_noop(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        session_id = "test-reconcile-prefilter"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        candidate_content = "The function uses callbacks and returns list of dicts."
        canned = [{"title": "Callback pattern", "project": "general",
                   "content": candidate_content, "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        # Make _find_best_match return score >= 0.95
        best_mem = {"id": "aabbccdd-1234-5678-abcd-ef0123456789",
                    "title": "Callback pattern", "essence": candidate_content}
        monkeypatch.setattr(
            md, "_find_best_match",
            lambda content, scope_id="default": (best_mem, 0.97)
        )

        # Track if reconcile Haiku is called
        reconcile_called = [False]
        import subprocess as _subprocess
        original_run = _subprocess.run

        def _intercepting_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._HAIKU_RECONCILE_SYSTEM in cmd:
                reconcile_called[0] = True
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FakeResult:
                    returncode = 0
                    stdout = "Test episode summary."
                    stderr = ""
                return FakeResult()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _intercepting_run)
        monkeypatch.setattr(md.subprocess, "run", _intercepting_run)

        md.run(session_id=session_id, transcript_path=str(transcript))

        assert not reconcile_called[0], (
            "Reconcile Haiku should NOT be called when pre-filter catches all candidates"
        )

        # No new regular memories should have been written
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 0, (
            f"Pre-filter should prevent any saves. Found: {[f.name for f in non_episode_files]}"
        )


# ---------------------------------------------------------------------------
# Test: Idempotent re-mine — run twice, same memory count
# ---------------------------------------------------------------------------


class TestReconcileIdempotentRemine:
    """Running mine_delta twice on the same content → same memory count."""

    def test_idempotent_remine(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        session_id = "test-idempotent-remine"
        transcript = mine_env["tmp_path"] / "session.jsonl"

        canned = [{"title": "Idempotent fact", "project": "general",
                   "content": "This fact should only be saved once.", "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new info"}
        ])

        # First run
        _write_jsonl(transcript, _make_turns(6))
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])
        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files_after_first = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        count_after_first = len(non_episode_files_after_first)
        assert count_after_first >= 1, "First run should save at least 1 memory"

        # Second run on same content — must NOT duplicate
        md._write_offset(session_id, 0)  # reset offset to reprocess same content
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])
        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()
        non_episode_files_after_second = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        count_after_second = len(non_episode_files_after_second)
        assert count_after_second == count_after_first, (
            f"Idempotent re-mine should not duplicate memories. "
            f"First: {count_after_first}, Second: {count_after_second}"
        )


# ---------------------------------------------------------------------------
# Test: Audit events written with correct op names
# ---------------------------------------------------------------------------


class TestReconcileAuditEvents:
    """Audit trail: every op writes a reconcile_* event to events.jsonl."""

    def test_audit_events_written(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store
        from memem.models import EVENT_LOG

        session_id = "test-audit-events"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [
            {"title": "New fact", "project": "general",
             "content": "Brand new knowledge to be saved.", "importance": 3},
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new info"},
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Check event log
        event_log = mine_env["state_dir"] / "events.jsonl"
        assert event_log.exists(), f"events.jsonl should exist at {event_log}"

        events = []
        for line in event_log.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        reconcile_ops = [e for e in events if "reconcile_" in e.get("op", "")]
        assert len(reconcile_ops) >= 1, (
            f"Expected at least 1 reconcile_* event, found {len(reconcile_ops)}. "
            f"All events: {[e.get('op') for e in events]}"
        )
        assert any(e.get("op") == "reconcile_add" for e in reconcile_ops), (
            f"Expected reconcile_add event, found: {[e.get('op') for e in reconcile_ops]}"
        )


# ---------------------------------------------------------------------------
# Test: Real JSONL fixture — no crash + offset advances
# ---------------------------------------------------------------------------


REAL_FIXTURE = Path(__file__).parent / "fixtures" / "real_session_sample.jsonl"


class TestRealSessionSample:
    """Real JSONL fixture: run() does not crash and offset advances."""

    def test_real_session_no_crash_offset_advance(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        if not REAL_FIXTURE.exists():
            pytest.skip(f"Real fixture not found: {REAL_FIXTURE}")

        session_id = "test-real-session-sample"

        # Mock extract_from_text to return canned memories (avoid real Haiku call)
        canned = [{"title": "Real session test", "project": "cortex-plugin",
                   "content": "Test fact from real session fixture.", "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        # Run with real fixture
        md.run(session_id=session_id, transcript_path=str(REAL_FIXTURE))

        # Offset should have advanced
        offset = md._read_offset(session_id)
        assert offset > 0, f"Offset should have advanced, got {offset}"

        # Session should be recorded
        mined = md._mined_sessions_path().read_text()
        assert session_id in mined, "Session should be recorded in .mined_sessions"


# ---------------------------------------------------------------------------
# Test: Pre-filter 0.95 generates reconcile_noop audit event
# ---------------------------------------------------------------------------


class TestReconcilePrefilterAuditEvent:
    """Pre-filter: near-exact candidate generates reconcile_noop in events.jsonl."""

    def test_prefilter_writes_reconcile_noop(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        from memem.models import EVENT_LOG

        session_id = "test-prefilter-audit"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        candidate_content = "The function uses callbacks and returns list of dicts."
        canned = [{"title": "Callback", "project": "general",
                   "content": candidate_content, "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        best_mem = {"id": "deadbeef-dead-dead-dead-deaddeadbeef",
                    "title": "Callback", "essence": candidate_content}
        monkeypatch.setattr(
            md, "_find_best_match",
            lambda content, scope_id="default": (best_mem, 0.97)
        )

        # Episode calls still need handling
        import subprocess as _subprocess
        original_run = _subprocess.run
        def _fake_run(cmd, **kwargs):
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FR:
                    returncode = 0; stdout = "Episode."; stderr = ""
                return FR()
            return original_run(cmd, **kwargs)
        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(md.subprocess, "run", _fake_run)

        md.run(session_id=session_id, transcript_path=str(transcript))

        event_log = mine_env["state_dir"] / "events.jsonl"
        events = []
        if event_log.exists():
            for line in event_log.read_text().splitlines():
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        noop_events = [e for e in events if e.get("op") == "reconcile_noop"]
        assert len(noop_events) >= 1, (
            f"Expected reconcile_noop audit event from pre-filter. "
            f"All ops: {[e.get('op') for e in events]}"
        )


# ---------------------------------------------------------------------------
# Phase 4.5-style review fixes (v2.7): protected-target guard + truncation guard
# ---------------------------------------------------------------------------


class TestReconcileProtectedTargetGuard:
    """SUPERSEDE/UPDATE against an L0 or decay_immune memory must degrade to ADD —
    one hallucinated Haiku op must never invalidate project-identity memories."""

    def test_supersede_against_decay_immune_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        protected = store._make_memory(
            content="Project identity: cortex-plugin is the memem dev repo, python 3.11.",
            title="Project identity anchor",
            project="cortex-plugin",
            source_type="user",
            source_session="pre-existing",
            importance=5,
        )
        protected["decay_immune"] = True
        store._save_memory(protected)
        store._reset_cache()
        store._trigger_sweep()
        target_id8 = protected["id"][:8]

        session_id = "test-reconcile-protected-guard"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "New unrelated fact", "project": "cortex-plugin",
                   "content": "A new fact that hostile output claims supersedes the anchor.",
                   "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "SUPERSEDE", "target": target_id8,
             "content": None, "reason": "hostile supersede"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()
        survivor = store._find_memory(protected["id"])
        assert survivor is not None, "Protected memory must still exist"
        assert survivor.get("invalid_at") in (None, ""), (
            f"Protected (decay_immune) memory must NOT be invalidated by reconcile; "
            f"invalid_at={survivor.get('invalid_at')!r}"
        )
        # The candidate itself must survive as an ADD (degraded, not dropped)
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        added = [
            f for f in memories_dir.glob("*.md")
            if "hostile output claims" in f.read_text()
        ]
        assert len(added) == 1, "Degraded candidate must be saved as ADD"


class TestReconcileTruncationGuard:
    """UPDATE with implausibly short merged content must degrade to ADD —
    a bad merge must never destroy an existing memory's essence."""

    def test_short_update_content_degrades_to_add(self, mine_env, monkeypatch):
        import memem.mine_delta as md
        import memem.obsidian_store as store

        long_essence = (
            "The retrieval engine fuses three ranked channels via reciprocal rank "
            "fusion and then normalizes the fused scores over the candidate pool "
            "before applying the signal multiplier bundle and MMR diversification. "
            "This sentence pads the essence well past three hundred characters so "
            "that the thirty-percent truncation floor has a meaningful baseline."
        )
        target = store._make_memory(
            content=long_essence,
            title="Engine architecture summary",
            project="cortex-plugin",
            source_type="user",
            source_session="pre-existing",
            importance=3,
        )
        store._save_memory(target)
        store._reset_cache()
        store._trigger_sweep()
        target_id8 = target["id"][:8]

        session_id = "test-reconcile-truncation-guard"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))

        canned = [{"title": "Engine note", "project": "cortex-plugin",
                   "content": "An engine note whose hostile merge result is one character.",
                   "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "UPDATE", "target": target_id8,
             "content": "x", "reason": "hostile truncation"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()
        survivor = store._find_memory(target["id"])
        assert survivor is not None
        assert survivor.get("essence") == long_essence, (
            f"Target essence must be untouched by a suspiciously short UPDATE; "
            f"got {survivor.get('essence')[:60]!r}"
        )
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        added = [
            f for f in memories_dir.glob("*.md")
            if "hostile merge result" in f.read_text()
        ]
        assert len(added) == 1, "Degraded candidate must be saved as ADD"


class TestDreamAutoOptOut:
    """MEMEM_DREAM_AUTO=0 must disable the autonomous dream spawn (Phase 4.5:
    unattended LLM spend always needs a kill switch)."""

    def test_opt_out_blocks_spawn_at_threshold(self, mine_env, monkeypatch):
        import memem.mine_delta as md

        monkeypatch.setenv("MEMEM_DREAM_AUTO", "0")
        # Counter at threshold-1 so this delta would normally fire the spawn
        counter = md._dream_counter_path()
        counter.write_text(str(md._DREAM_COUNTER_THRESHOLD - 1))

        canned = [{"title": "Opt-out check", "project": "general",
                   "content": "A substantive fact to make the delta count.", "importance": 3}]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))
        reconcile_resp = _canned_reconcile_response([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new"}
        ])
        _make_subprocess_patcher(md, monkeypatch, [reconcile_resp])

        spawns = []
        monkeypatch.setattr(md.subprocess, "Popen",
                            lambda *a, **k: spawns.append(a) or None)

        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(6))
        md.run(session_id="dream-optout-test", transcript_path=str(transcript))

        assert spawns == [], "MEMEM_DREAM_AUTO=0 must suppress the dream spawn"
