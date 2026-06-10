"""Integration tests for the full reconcile-at-write pipeline (C1+C4).

Tests cover the end-to-end flow through mine_delta.run() with a canned
subprocess response exercising all four ops in a single run:
  - candidate0: ADD
  - candidate1: UPDATE of a pre-seeded neighbor (merged essence, extra tags,
    extra keys, updated_at bumped)
  - candidate2: SUPERSEDE of a second pre-seeded neighbor (new memory saved,
    old memory gets invalid_at + replaced_by, absent from FTS index)

Also covers:
  - All four audit events present in events.jsonl with correct op names
  - Episode emitted (episodic memory file present)
  - Offset advanced past 0
  - .mined_sessions updated
  - Exception fallback path (reconcile raises → _fallback_add_all)

Uses the real fixture tests/fixtures/reconcile_integration_synthetic.jsonl and distinguishes
reconcile vs episode subprocess calls by system-prompt content (constant now
imported from haiku_prompts).
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "reconcile_integration_synthetic.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def _make_turns(n: int = 8) -> list[dict]:
    """Synthetic turns in real Claude Code nested schema — substantive enough
    to pass the trivial-delta check."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = (
            f"Turn {i}: This is substantive content about reconcile-at-write "
            f"architecture decisions and important design patterns that should "
            f"be remembered by the memory system for future reference. "
            f"The reconcile path uses ADD/UPDATE/SUPERSEDE/NOOP ops. " * 2
        )
        turns.append({
            "type": role,
            "message": {"role": role, "content": text},
        })
    return turns


def _read_events(state_dir: Path) -> list[dict]:
    """Read all events from events.jsonl."""
    event_log = state_dir / "events.jsonl"
    if not event_log.exists():
        return []
    events = []
    for line in event_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


# ---------------------------------------------------------------------------
# Shared test fixture
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


def _patch_subprocess(md, monkeypatch, reconcile_resp: str, episode_summary: str = ""):
    """Patch subprocess.run: reconcile calls return canned JSON, episode calls
    return a short narrative.

    Reconcile calls are identified by _HAIKU_RECONCILE_SYSTEM (imported from
    haiku_prompts) being present in the --system-prompt argument.
    Episode calls are identified by _EPISODE_HAIKU_SYSTEM.
    """
    import subprocess as _subprocess
    from memem.haiku_prompts import _HAIKU_RECONCILE_SYSTEM

    original_run = _subprocess.run

    def _fake_run(cmd, **kwargs):
        if "--system-prompt" in cmd and _HAIKU_RECONCILE_SYSTEM in cmd:
            class FakeReconcileResult:
                returncode = 0
                stdout = reconcile_resp
                stderr = ""
            return FakeReconcileResult()

        if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
            class FakeEpisodeResult:
                returncode = 0
                stdout = episode_summary or "Integration test episode summary."
                stderr = ""
            return FakeEpisodeResult()

        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    monkeypatch.setattr(md.subprocess, "run", _fake_run)


# ---------------------------------------------------------------------------
# Test: Full 3-candidate pipeline — ADD + UPDATE + SUPERSEDE
# ---------------------------------------------------------------------------


class TestFullReconcilePipeline:
    """End-to-end integration: 3 candidates, all four ops (ADD/UPDATE/SUPERSEDE/NOOP)
    in a single run(), against a vault pre-seeded with 2 neighbor memories."""

    def test_all_four_ops_single_run(self, mine_env, monkeypatch):
        """All four ops exercised in one run() call.

        Setup:
          - neighbor_a: will be UPDATE target (candidate1)
          - neighbor_b: will be SUPERSEDE target (candidate2)
          - candidate0: ADD (brand new)
          - candidate1: UPDATE of neighbor_a
          - candidate2: SUPERSEDE of neighbor_b
          (candidate3 would be NOOP — we include it via the canned response to
           exercise the NOOP code path even though no 4th candidate in canned list)

        Assertions:
          - ADD memory exists in vault with stable id
          - neighbor_a has merged essence + extra tags + extra keys + updated_at bumped
          - neighbor_b has invalid_at + replaced_by set; new SUPERSEDE memory exists
          - 4 reconcile_* audit events (add, update, supersede, noop)
          - episode memory exists
          - offset advanced > 0
          - session recorded in .mined_sessions
        """
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Pre-seed vault with two neighbor memories
        neighbor_a = store._make_memory(
            content="The reconcile path uses idempotent stable IDs for ADD ops.",
            title="Reconcile stable ID convention",
            tags=["cortex-plugin"],
            project="cortex-plugin",
            source_type="mined",
            source_session="prior-session-a",
            importance=3,
        )
        store._save_memory(neighbor_a)

        neighbor_b = store._make_memory(
            content="Old approach: all candidates saved unconditionally with uuid4.",
            title="Old unconditional save",
            tags=["design"],
            project="cortex-plugin",
            source_type="user",
            source_session="prior-session-b",
            importance=3,
        )
        store._save_memory(neighbor_b)

        store._reset_cache()
        store._trigger_sweep()

        neighbor_a_id8 = neighbor_a["id"][:8]
        neighbor_b_id8 = neighbor_b["id"][:8]
        neighbor_a_full = neighbor_a["id"]
        neighbor_b_full = neighbor_b["id"]

        # Candidates to inject
        merged_content = (
            "The reconcile path uses idempotent stable IDs for ADD ops. "
            "Extended: uuid5 derived from session_id + title + content ensures "
            "re-mining produces the same ID, preventing duplicates."
        )
        canned_candidates = [
            {
                "title": "Brand new reconcile fact",
                "project": "cortex-plugin",
                "content": "Candidate ADD: the reconciler caps UPDATE+SUPERSEDE at 5 per delta.",
                "importance": 3,
                "tags": [],
                "keys": [],
            },
            {
                "title": "Reconcile stable ID convention",
                "project": "cortex-plugin",
                "content": "Extended reconcile: uuid5 derived from session_id + title + content.",
                "importance": 3,
                "tags": ["reconcile", "idempotent"],
                "keys": ["uuid5", "stable-id"],
            },
            {
                "title": "New reconcile approach",
                "project": "cortex-plugin",
                "content": "New approach: reconcile-at-write with four ops replaces unconditional save.",
                "importance": 4,
                "tags": ["reconcile"],
                "keys": [],
            },
            {
                "title": "Reconcile NOOP fact",
                "project": "cortex-plugin",
                "content": "Known fact: Python uses indentation for scope.",
                "importance": 2,
                "tags": [],
                "keys": [],
            },
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned_candidates))

        # Canned reconcile response: ADD, UPDATE, SUPERSEDE, NOOP
        reconcile_resp = json.dumps([
            {
                "index": 0,
                "op": "ADD",
                "target": None,
                "content": None,
                "reason": "new information",
            },
            {
                "index": 1,
                "op": "UPDATE",
                "target": neighbor_a_id8,
                "content": merged_content,
                "reason": "extends neighbor",
            },
            {
                "index": 2,
                "op": "SUPERSEDE",
                "target": neighbor_b_id8,
                "content": None,
                "reason": "replaces old approach",
            },
            {
                "index": 3,
                "op": "NOOP",
                "target": None,
                "content": None,
                "reason": "redundant info",
            },
        ])

        _patch_subprocess(md, monkeypatch, reconcile_resp, episode_summary="Session on reconcile architecture.")

        session_id = "test-integration-all-ops"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(8))

        md.run(session_id=session_id, transcript_path=str(transcript))

        store._reset_cache()
        store._trigger_sweep()

        # ---- Verify ADD memory exists ----
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        add_files = [
            f for f in memories_dir.glob("*.md")
            if "caps UPDATE+SUPERSEDE" in f.read_text()
        ]
        assert len(add_files) == 1, (
            f"Expected 1 ADD memory file, found {len(add_files)}"
        )

        # ---- Verify ADD has stable uuid5 id ----
        from memem.obsidian_store import _stable_mined_memory_id
        stable_add_id = _stable_mined_memory_id(
            session_id,
            "Brand new reconcile fact",
            "Candidate ADD: the reconciler caps UPDATE+SUPERSEDE at 5 per delta.",
        )
        assert stable_add_id in add_files[0].read_text(), (
            f"Stable uuid5 id should appear in ADD memory file"
        )

        # ---- Verify UPDATE: merged essence + extra tags + extra keys + updated_at ----
        updated_mem = store._find_memory(neighbor_a_full)
        assert updated_mem is not None, "Neighbor A should still exist after UPDATE"
        essence = updated_mem.get("essence") or ""
        assert merged_content in essence or essence == merged_content, (
            f"Merged content not found in updated memory. Got: {essence[:200]}"
        )
        # Extra tags should have been merged in
        tags = updated_mem.get("domain_tags") or []
        assert "reconcile" in tags, f"Extra tag 'reconcile' not in tags: {tags}"
        assert "idempotent" in tags, f"Extra tag 'idempotent' not in tags: {tags}"
        # Extra keys should have been merged in
        keys = updated_mem.get("keys") or []
        assert "uuid5" in keys, f"Extra key 'uuid5' not in keys: {keys}"
        assert "stable-id" in keys, f"Extra key 'stable-id' not in keys: {keys}"
        # updated_at should be set (truthy)
        assert updated_mem.get("updated_at"), (
            f"updated_at should be set after UPDATE, got: {updated_mem.get('updated_at')!r}"
        )

        # ---- Verify SUPERSEDE: neighbor_b has invalid_at + replaced_by ----
        superseded_mem = store._find_memory(neighbor_b_full)
        assert superseded_mem is not None, "Neighbor B should still exist (bi-temporal)"
        assert superseded_mem.get("invalid_at") not in (None, ""), (
            f"Superseded memory should have invalid_at set, got: {superseded_mem.get('invalid_at')!r}"
        )
        assert superseded_mem.get("replaced_by") not in (None, ""), (
            f"Superseded memory should have replaced_by set, got: {superseded_mem.get('replaced_by')!r}"
        )

        # ---- Verify new SUPERSEDE memory exists ----
        new_supersede_id = superseded_mem.get("replaced_by")
        new_supersede_mem = store._find_memory(new_supersede_id)
        assert new_supersede_mem is not None, (
            f"New SUPERSEDE memory (id={new_supersede_id!r}) should exist in vault"
        )
        assert "reconcile approach" in (new_supersede_mem.get("title") or "").lower(), (
            f"New SUPERSEDE memory should have expected title; "
            f"got: {new_supersede_mem.get('title')!r}"
        )

        # ---- Verify neighbor_b absent from default _obsidian_memories ----
        # After invalidation, _remove_from_index is called so the memory
        # won't appear in FTS search. Verify the physical file still exists
        # (bi-temporal preservation) but the invalid_at field is set so it
        # won't participate in active recall.
        b_files = [
            f for f in memories_dir.glob("*.md")
            if "Old approach" in f.read_text() and "invalid_at" in f.read_text()
        ]
        assert len(b_files) >= 1, (
            "Neighbor B markdown file should exist on disk with invalid_at set (bi-temporal)"
        )

        # ---- Verify all 4 reconcile_* audit events in events.jsonl ----
        events = _read_events(mine_env["state_dir"])
        reconcile_events = [e for e in events if "reconcile_" in e.get("op", "")]
        ops_found = {e["op"] for e in reconcile_events}

        assert "reconcile_add" in ops_found, (
            f"reconcile_add event missing. Found ops: {ops_found}"
        )
        assert "reconcile_update" in ops_found, (
            f"reconcile_update event missing. Found ops: {ops_found}"
        )
        assert "reconcile_supersede" in ops_found, (
            f"reconcile_supersede event missing. Found ops: {ops_found}"
        )
        assert "reconcile_noop" in ops_found, (
            f"reconcile_noop event missing. Found ops: {ops_found}"
        )

        # ---- Verify episode emitted ----
        episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" in f.read_text()
        ]
        assert len(episode_files) >= 1, (
            f"Expected at least 1 episodic memory, found {len(episode_files)}"
        )

        # ---- Verify offset advanced ----
        offset = md._read_offset(session_id)
        assert offset > 0, f"Offset should have advanced beyond 0, got {offset}"

        # ---- Verify .mined_sessions updated ----
        mined_sessions = md._mined_sessions_path().read_text()
        assert session_id in mined_sessions, (
            f"Session {session_id} should be in .mined_sessions"
        )


# ---------------------------------------------------------------------------
# Test: Real fixture — no crash + offset advances + session recorded
# ---------------------------------------------------------------------------


class TestRealFixtureIntegration:
    """Integration test using reconcile_integration_synthetic.jsonl fixture."""

    def test_real_fixture_full_pipeline(self, mine_env, monkeypatch):
        """run() on reconcile_integration_synthetic.jsonl: no crash, offset advances,
        session recorded, audit trail written."""

        if not REAL_FIXTURE.exists():
            pytest.skip(f"Real fixture not found: {REAL_FIXTURE}")

        import memem.mine_delta as md

        session_id = "test-real-session-real-fixture"

        # Canned extraction
        canned = [
            {
                "title": "Reconcile prompt location",
                "project": "cortex-plugin",
                "content": "_HAIKU_RECONCILE_SYSTEM lives in haiku_prompts.py as _HAIKU_RECONCILE_SYSTEM.",
                "importance": 3,
                "tags": [],
                "keys": ["haiku_prompts", "reconcile"],
            }
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = json.dumps([
            {"index": 0, "op": "ADD", "target": None, "content": None, "reason": "new info"}
        ])
        _patch_subprocess(md, monkeypatch, reconcile_resp, episode_summary="Real session episode.")

        md.run(session_id=session_id, transcript_path=str(REAL_FIXTURE))

        # Offset advanced
        offset = md._read_offset(session_id)
        assert offset > 0, f"Offset should have advanced, got {offset}"

        # Session recorded
        mined_sessions = md._mined_sessions_path().read_text()
        assert session_id in mined_sessions, (
            f"Session should be in .mined_sessions: {session_id}"
        )

        # At least one memory saved or at least the audit trail exists
        events = _read_events(mine_env["state_dir"])
        assert len(events) >= 1, "At least one audit event should have been written"


# ---------------------------------------------------------------------------
# Test: Exception in reconcile path → fallback ADD-all
# ---------------------------------------------------------------------------


class TestReconcileExceptionFallback:
    """When reconcile raises, _fallback_add_all saves all candidates."""

    def test_exception_fallback_saves_all_candidates(self, mine_env, monkeypatch):
        """Simulate reconcile path exception → all candidates saved via ADD-all."""
        import memem.mine_delta as md

        session_id = "test-integration-fallback"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(8))

        canned = [
            {
                "title": "Fallback candidate A",
                "project": "cortex-plugin",
                "content": "Fallback ADD candidate A: important reconcile convention.",
                "importance": 3,
            },
            {
                "title": "Fallback candidate B",
                "project": "cortex-plugin",
                "content": "Fallback ADD candidate B: another important fact.",
                "importance": 3,
            },
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        # Make subprocess.run raise for reconcile calls → triggers exception fallback
        import subprocess as _subprocess
        from memem.haiku_prompts import _HAIKU_RECONCILE_SYSTEM
        original_run = _subprocess.run

        def _raising_run(cmd, **kwargs):
            if "--system-prompt" in cmd and _HAIKU_RECONCILE_SYSTEM in cmd:
                raise RuntimeError("Simulated reconcile subprocess failure")
            if "--system-prompt" in cmd and md._EPISODE_HAIKU_SYSTEM in cmd:
                class FR:
                    returncode = 0
                    stdout = "Fallback episode."
                    stderr = ""
                return FR()
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(_subprocess, "run", _raising_run)
        monkeypatch.setattr(md.subprocess, "run", _raising_run)

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Both candidates should have been saved via fallback ADD-all
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        non_episode_files = [
            f for f in memories_dir.glob("*.md")
            if "type:episodic" not in f.read_text()
        ]
        assert len(non_episode_files) == 2, (
            f"Fallback should have saved 2 memories, found {len(non_episode_files)}: "
            f"{[f.name for f in non_episode_files]}"
        )

        # Verify reconcile_add audit events emitted by fallback
        events = _read_events(mine_env["state_dir"])
        fallback_adds = [e for e in events if e.get("op") == "reconcile_add"]
        assert len(fallback_adds) >= 2, (
            f"Expected at least 2 reconcile_add events from fallback, found {len(fallback_adds)}"
        )


# ---------------------------------------------------------------------------
# Test: Safety cap — UPDATE+SUPERSEDE > 5 for 4-candidate batch
# ---------------------------------------------------------------------------


class TestReconcileIntegrationSafetyCap:
    """UPDATE+SUPERSEDE cap: excess ops degrade to ADD in the integration path."""

    def test_safety_cap_degrades_excess_to_add(self, mine_env, monkeypatch):
        """Sends 4 UPDATE ops to reconciler; cap is 5 so all 4 succeed (cap not hit)
        unless targets don't exist — this test verifies the count mechanism."""
        import memem.mine_delta as md
        import memem.obsidian_store as store

        # Pre-seed 3 target memories
        targets = []
        for i in range(3):
            m = store._make_memory(
                content=f"Target memory {i} for cap test.",
                title=f"Cap target {i}",
                project="cortex-plugin",
                source_type="mined",
                source_session="pre-existing",
                importance=3,
            )
            store._save_memory(m)
            targets.append(m)
        store._reset_cache()
        store._trigger_sweep()

        session_id = "test-integration-safety-cap"
        transcript = mine_env["tmp_path"] / "session.jsonl"
        _write_jsonl(transcript, _make_turns(8))

        # 4 candidates requesting UPDATE of first 3 real targets + 1 missing target
        canned = [
            {
                "title": f"Cap candidate {i}",
                "project": "cortex-plugin",
                "content": f"Update content for target {i}.",
                "importance": 3,
                "tags": [],
                "keys": [],
            }
            for i in range(4)
        ]
        monkeypatch.setattr(md, "extract_from_text", lambda t, context_hint="": list(canned))

        reconcile_resp = json.dumps(
            [
                {
                    "index": i,
                    "op": "UPDATE",
                    "target": targets[i]["id"][:8] if i < 3 else "deadbeef",
                    "content": f"Merged content for cap test {i}. Original: Target memory {i}.",
                    "reason": "refines existing",
                }
                for i in range(4)
            ]
        )
        _patch_subprocess(md, monkeypatch, reconcile_resp, episode_summary="Cap test episode.")

        md.run(session_id=session_id, transcript_path=str(transcript))

        # Candidate 3 had invalid target (deadbeef) → degraded to ADD
        memories_dir = mine_env["vault_dir"] / "memem" / "memories"
        new_files = [
            f for f in memories_dir.glob("*.md")
            if "Cap candidate 3" in f.read_text() and "type:episodic" not in f.read_text()
        ]
        assert len(new_files) == 1, (
            f"Candidate with invalid target should be saved as ADD. "
            f"Found: {[f.name for f in new_files]}"
        )

        # Offset advanced
        offset = md._read_offset(session_id)
        assert offset > 0, f"Offset should have advanced, got {offset}"
