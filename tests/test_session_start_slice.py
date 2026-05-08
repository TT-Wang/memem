"""Tests for m1: generate_session_start_slice (memem v1.5.0).

Covers:
  1. Output differs structurally from generate_active_memory_slice / generate_prompt_context
  2. Prior working_memory.md content appears in output when file exists
  3. Decision selection respects recency+access weight ordering
  4. L0 anchors always present
  5. Active arcs filtered correctly
  6. Compaction checkpoint from last 24h included when present
  7. Char budget honored
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mem(
    title: str,
    content: str,
    tags: list[str] | None = None,
    project: str = "test-project",
    layer: int = 2,
    arc_id: str = "",
    closed: bool = False,
    access_count: int = 0,
    created_at: str = "",
    last_accessed_at: str = "",
) -> dict[str, Any]:
    """Build a synthetic memory dict that matches the obsidian_store schema."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    mem: dict[str, Any] = {
        "id": f"{title.replace(' ', '-').lower()[:16]}-{'x' * 8}",
        "title": title,
        "essence": content,
        "full_record": content,
        "domain_tags": list(tags or []),
        "project": project,
        "source_type": "user",
        "source_session": "",
        "importance": 3,
        "created_at": created_at or now_str,
        "updated_at": now_str,
        "last_accessed_at": last_accessed_at or created_at or now_str,
        "access_count": access_count,
        "layer": layer,
        "schema_version": 1,
        "status": "active",
        "invalid_at": None,
        "replaced_by": None,
        "decay_immune": False,
    }
    if arc_id:
        mem["arc_id"] = arc_id
    if closed:
        mem["closed"] = True
    return mem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_engine(tmp_vault, tmp_cortex_dir):
    """Reload active_slice_engine in a clean tmp vault."""
    import memem.obsidian_store as obs
    importlib.reload(obs)
    from memem import active_slice_engine
    importlib.reload(active_slice_engine)
    return active_slice_engine


# ---------------------------------------------------------------------------
# Test 1: Output differs structurally from generate_active_memory_slice
# ---------------------------------------------------------------------------


def test_session_start_slice_differs_structurally(tmp_vault, tmp_cortex_dir, monkeypatch):
    """generate_session_start_slice output has different section headers than generate_prompt_context."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.obsidian_store as obs
    importlib.reload(obs)
    from memem.active_slice_engine import generate_session_start_slice

    result = generate_session_start_slice(
        scope_id="test-project",
        session_id="sess-struct-test",
    )

    # The new function must contain at least one of the known section headers
    # that generate_prompt_context does NOT emit.
    session_start_markers = [
        "Prior working memory",
        "Recent decisions",
        "Active arcs",
        "L0 anchors",
        "Compaction checkpoint",
    ]
    assert any(marker in result for marker in session_start_markers), (
        f"Output lacks session-start section headers. Got:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Prior working_memory.md content appears in output when file exists
# ---------------------------------------------------------------------------


def test_working_memory_content_in_output(tmp_vault, tmp_cortex_dir, monkeypatch):
    """working_memory.md content appears in generate_session_start_slice output."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.working_memory as wm_mod
    import memem.obsidian_store as obs
    importlib.reload(obs)

    wm_file = tmp_cortex_dir / "working_memory.md"
    sections = {
        "current_task": "Implement the boundary crossing feature for v1.5.0",
        "active_hypothesis": "",
        "last_3_actions": "",
        "stuck_on": "",
        "decided_this_session": "",
    }
    wm_mod.write_working_memory.__wrapped__ if hasattr(wm_mod.write_working_memory, "__wrapped__") else None
    # Write directly to the tmp file
    text = wm_mod.serialize_to_md(sections)
    wm_file.write_text(text, encoding="utf-8")

    from memem.active_slice_engine import generate_session_start_slice

    result = generate_session_start_slice(
        scope_id="test-project",
        session_id="sess-wm-test",
        memem_dir=str(tmp_cortex_dir),
    )

    assert "Implement the boundary crossing feature" in result, (
        f"Working memory content missing from output:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: Decision selection respects recency+access weight ordering
# ---------------------------------------------------------------------------


def test_decision_ranking_by_recency_and_access(tmp_vault, tmp_cortex_dir, monkeypatch):
    """Top-3 decisions are selected by access_count * recency_decay, not raw order."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.obsidian_store as obs
    importlib.reload(obs)

    now = datetime.now(UTC)

    # Create 5 synthetic decision memories with controlled timestamps and access_counts.
    # Expected ranking by compute_strength:
    #   - high_access_recent: access_count=20, 1 day old  -> highest strength
    #   - medium_access_recent: access_count=5, 2 days old
    #   - medium_access_mid: access_count=5, 4 days old
    #   - low_access_recent: access_count=1, 1 day old
    #   - zero_access_old: access_count=0, 6 days old    -> lowest strength

    mems_data = [
        ("high_access_recent",    20, now - timedelta(days=1)),
        ("medium_access_recent",  5,  now - timedelta(days=2)),
        ("medium_access_mid",     5,  now - timedelta(days=4)),
        ("low_access_recent",     1,  now - timedelta(days=1)),
        ("zero_access_old",       0,  now - timedelta(days=6)),
    ]

    synthetic_mems = []
    for name, acc, ts in mems_data:
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        m = _make_mem(
            title=f"Decision {name}",
            content=f"This is the {name} decision content, long enough to be useful.",
            tags=["decision"],
            project="test-project",
            access_count=acc,
            created_at=ts_str,
            last_accessed_at=ts_str,
        )
        synthetic_mems.append(m)

    with mock.patch("memem.obsidian_store._obsidian_memories", return_value=synthetic_mems):
        from memem.active_slice_engine import _render_decisions_block
        block = _render_decisions_block("test-project")

    # Top 3 should include the highest-ranked by strength.
    assert "high_access_recent" in block, (
        f"Expected 'high_access_recent' in top-3 decisions.\nGot:\n{block}"
    )
    # zero_access_old should NOT appear (it's #5 by strength)
    assert "zero_access_old" not in block, (
        f"'zero_access_old' should not appear in top-3.\nGot:\n{block}"
    )


# ---------------------------------------------------------------------------
# Test 4: L0 anchors always present in output
# ---------------------------------------------------------------------------


def test_l0_anchors_always_present(tmp_vault, tmp_cortex_dir, monkeypatch):
    """L0 anchors section always appears in generate_session_start_slice output."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.obsidian_store as obs
    importlib.reload(obs)

    from memem.active_slice_engine import generate_session_start_slice

    result = generate_session_start_slice(
        scope_id="test-project",
        session_id="sess-l0-test",
    )

    assert "L0 anchors" in result, (
        f"L0 anchors section missing from output:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Active arcs filtered correctly
# ---------------------------------------------------------------------------


def test_active_arcs_filtered_correctly(tmp_vault, tmp_cortex_dir, monkeypatch):
    """Only arc_id-set, non-closed memories appear in active arcs section."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.obsidian_store as obs
    importlib.reload(obs)

    # Three memories:
    # 1. has arc_id + not closed -> SHOULD appear
    # 2. has arc_id + closed     -> should NOT appear
    # 3. no arc_id               -> should NOT appear
    synthetic_mems = [
        _make_mem(
            title="Open arc memory",
            content="This arc is open and active.",
            tags=[],
            project="test-project",
            arc_id="arc-001",
            closed=False,
        ),
        _make_mem(
            title="Closed arc memory",
            content="This arc is closed.",
            tags=["closed"],
            project="test-project",
            arc_id="arc-002",
            closed=True,
        ),
        _make_mem(
            title="No arc memory",
            content="This memory has no arc_id.",
            tags=[],
            project="test-project",
            arc_id="",
            closed=False,
        ),
    ]

    with mock.patch("memem.obsidian_store._obsidian_memories", return_value=synthetic_mems):
        from memem.active_slice_engine import _render_active_arcs_block
        block = _render_active_arcs_block("test-project")

    assert "Open arc memory" in block, (
        f"Open arc memory should appear in arcs block.\nGot:\n{block}"
    )
    assert "Closed arc memory" not in block, (
        f"Closed arc memory should NOT appear.\nGot:\n{block}"
    )
    assert "No arc memory" not in block, (
        f"Memory without arc_id should NOT appear.\nGot:\n{block}"
    )


# ---------------------------------------------------------------------------
# Test 6: Compaction checkpoint from last 24h included when present
# ---------------------------------------------------------------------------


def test_compaction_checkpoint_included(tmp_vault, tmp_cortex_dir, monkeypatch):
    """Compaction checkpoint from last 24h appears in session-start output."""
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", "8000")
    import memem.obsidian_store as obs
    importlib.reload(obs)

    from memem.compaction import build_compaction_snapshot, save_compaction_checkpoint

    # Write a minimal transcript file
    transcript = tmp_cortex_dir / "test-transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    # Build + save a real checkpoint via m2's API
    snapshot = build_compaction_snapshot(
        session_id="sess-cp-test",
        transcript_path=str(transcript),
        memem_dir=tmp_cortex_dir,
    )
    mem_id = save_compaction_checkpoint(
        snapshot=snapshot,
        session_id="sess-cp-test",
        project_id="test-project",
    )
    assert mem_id, "save_compaction_checkpoint should return a non-empty memory_id"

    # Force cache refresh
    obs._ensure_cache_warm()

    from memem.active_slice_engine import generate_session_start_slice

    result = generate_session_start_slice(
        scope_id="test-project",
        session_id="sess-cp-test",
    )

    assert "Compaction checkpoint" in result, (
        f"Compaction checkpoint section missing from output:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: Char budget honored
# ---------------------------------------------------------------------------


def test_char_budget_honored(tmp_vault, tmp_cortex_dir, monkeypatch):
    """Output length stays within MEMEM_SESSION_START_BUDGET chars."""
    budget = 500
    monkeypatch.setenv("MEMEM_SESSION_START_BUDGET", str(budget))
    import memem.obsidian_store as obs
    importlib.reload(obs)

    from memem.active_slice_engine import generate_session_start_slice

    # Write a large working memory to stress the budget
    import memem.working_memory as wm_mod
    wm_file = tmp_cortex_dir / "working_memory.md"
    big_sections = {
        "current_task": "A" * 1000,
        "active_hypothesis": "B" * 1000,
        "last_3_actions": "C" * 1000,
        "stuck_on": "D" * 1000,
        "decided_this_session": "E" * 1000,
    }
    text = wm_mod.serialize_to_md(big_sections)
    wm_file.write_text(text, encoding="utf-8")

    result = generate_session_start_slice(
        scope_id="test-project",
        session_id="sess-budget-test",
        memem_dir=str(tmp_cortex_dir),
    )

    assert len(result) <= budget, (
        f"Output exceeded budget ({budget} chars). Got {len(result)} chars."
    )
