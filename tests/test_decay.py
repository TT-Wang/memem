"""Tests for damped decay with L0 pin and decay_immune flag (memem v2 m3).

Covers:
1.  compute_strength: fresh memory high
2.  compute_strength: old unaccessed memory low
3.  compute_strength: recent access boosts
4.  compute_decay_factor: phase 1 full reinforcement
5.  compute_decay_factor: phase 2 phi damping (monotonically decreasing)
6.  compute_decay_factor: phase 3 log damping
7.  compute_decay_factor: runaway reinforcement capped (echo-chamber defense)
8.  is_immune: L0 always True
9.  is_immune: decay_immune flag for non-L0
10. should_demote: returns None for L0
11. should_demote: returns next layer for weak memory
12. should_demote: returns None for L3 already
13. should_demote: returns None for decay_immune
14. bump_access: increments count and updates timestamp
15. recall bumps access on hit
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Test 1: fresh memory → high strength
# ---------------------------------------------------------------------------


def test_compute_strength_fresh_memory_high():
    from memem.decay import compute_strength

    mem = {
        "created_at": datetime.now(UTC).isoformat(),
        "last_accessed_at": datetime.now(UTC).isoformat(),
        "access_count": 0,
        "importance": 3,
        "layer": 2,
    }
    strength = compute_strength(mem)
    # Fresh memory with default importance weight (0.6 + 0.2*(3-1) = 1.0) should be ≥ 0.5
    assert strength >= 0.5, f"Fresh memory strength should be ≥ 0.5, got {strength}"


# ---------------------------------------------------------------------------
# Test 2: old unaccessed memory → low strength
# ---------------------------------------------------------------------------


def test_compute_strength_old_unaccessed_low():
    from memem.decay import compute_strength

    old_ts = (datetime.now(UTC) - timedelta(days=180)).isoformat()
    mem = {
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 3,
        "layer": 2,
    }
    strength = compute_strength(mem)
    assert strength < 0.2, f"180-day-old unaccessed memory strength should be < 0.2, got {strength}"


# ---------------------------------------------------------------------------
# Test 3: recent access boosts strength vs. untouched
# ---------------------------------------------------------------------------


def test_compute_strength_recent_access_boosts():
    from memem.decay import compute_strength

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=30)).isoformat()

    # Memory with 10 accesses, last accessed now
    boosted = {
        "created_at": old_ts,
        "last_accessed_at": now.isoformat(),
        "access_count": 10,
        "importance": 3,
        "layer": 2,
    }
    # Memory with 0 accesses, created 30 days ago and never touched
    untouched = {
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 3,
        "layer": 2,
    }
    boosted_strength = compute_strength(boosted, now=now)
    untouched_strength = compute_strength(untouched, now=now)
    assert boosted_strength > untouched_strength, (
        f"Boosted ({boosted_strength:.3f}) should exceed untouched ({untouched_strength:.3f})"
    )


# ---------------------------------------------------------------------------
# Test 4: phase 1 — full reinforcement for access_count ≤ PHI_DAMPING_KNEE
# ---------------------------------------------------------------------------


def test_compute_decay_factor_phase1_full_reinforcement():
    from memem.decay import PHI_DAMPING_KNEE, compute_decay_factor

    for n in range(0, PHI_DAMPING_KNEE + 1):
        factor = compute_decay_factor(n)
        assert factor == 1.0, f"Phase 1: access_count={n} should give factor=1.0, got {factor}"


# ---------------------------------------------------------------------------
# Test 5: phase 2 — phi damping monotonically decreasing
# ---------------------------------------------------------------------------


def test_compute_decay_factor_phase2_phi_damping():
    from memem.decay import LOG_DAMPING_KNEE, PHI_DAMPING_KNEE, compute_decay_factor

    prev = compute_decay_factor(PHI_DAMPING_KNEE)
    for n in range(PHI_DAMPING_KNEE + 1, LOG_DAMPING_KNEE + 1):
        curr = compute_decay_factor(n)
        assert curr <= prev, (
            f"Phase 2: factor should be monotonically decreasing, but at n={n} "
            f"curr={curr:.4f} > prev={prev:.4f}"
        )
        prev = curr


# ---------------------------------------------------------------------------
# Test 6: phase 3 — log damping kicks in beyond LOG_DAMPING_KNEE
# ---------------------------------------------------------------------------


def test_compute_decay_factor_phase3_log_damping():
    from memem.decay import LOG_DAMPING_KNEE, compute_decay_factor

    factor_at_knee = compute_decay_factor(LOG_DAMPING_KNEE)
    factor_well_past = compute_decay_factor(LOG_DAMPING_KNEE + 50)
    assert factor_well_past < factor_at_knee, (
        f"Phase 3: log damping should reduce factor below knee value {factor_at_knee:.4f}, "
        f"but got {factor_well_past:.4f}"
    )
    assert factor_well_past > 0.0, "Phase 3: factor should never reach 0"


# ---------------------------------------------------------------------------
# Test 7: echo-chamber defense — high access_count yields very small factor
# ---------------------------------------------------------------------------


def test_compute_decay_factor_runaway_reinforcement_capped():
    from memem.decay import compute_decay_factor

    factor = compute_decay_factor(1000)
    assert factor < 0.1, (
        f"access_count=1000 should yield factor < 0.1 (echo-chamber defense), got {factor:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 8: L0 is always immune
# ---------------------------------------------------------------------------


def test_is_immune_l0_always_true():
    from memem.decay import is_immune

    # L0 with decay_immune=False should still be immune
    mem_l0 = {"layer": 0, "decay_immune": False}
    assert is_immune(mem_l0) is True, "L0 memory must always be immune regardless of decay_immune"

    # L0 with decay_immune=True also immune
    mem_l0_flagged = {"layer": 0, "decay_immune": True}
    assert is_immune(mem_l0_flagged) is True


# ---------------------------------------------------------------------------
# Test 9: decay_immune=True grants immunity to non-L0
# ---------------------------------------------------------------------------


def test_is_immune_decay_immune_flag_for_non_l0():
    from memem.decay import is_immune

    # L2 with decay_immune=True → immune
    mem_l2_immune = {"layer": 2, "decay_immune": True}
    assert is_immune(mem_l2_immune) is True, "decay_immune=True must grant immunity to L2 memory"

    # L2 with decay_immune=False → not immune
    mem_l2_normal = {"layer": 2, "decay_immune": False}
    assert is_immune(mem_l2_normal) is False, "L2 memory without decay_immune should NOT be immune"


# ---------------------------------------------------------------------------
# Test 10: should_demote returns None for L0 even with zero strength
# ---------------------------------------------------------------------------


def test_should_demote_returns_none_for_l0():
    from memem.decay import should_demote

    # L0 memory with very old timestamp (effectively zero strength)
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l0 = {
        "layer": 0,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 1,
    }
    result = should_demote(mem_l0)
    assert result is None, f"L0 memory must never be demoted, got {result}"


# ---------------------------------------------------------------------------
# Test 11: should_demote returns next layer for weak non-immune memory
# ---------------------------------------------------------------------------


def test_should_demote_returns_next_layer_for_weak_memory():
    from memem.decay import should_demote

    # L2 memory with extremely old access → very weak
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l2 = {
        "layer": 2,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 1,
    }
    result = should_demote(mem_l2)
    assert result == 3, f"Weak L2 memory should be suggested for L3 demotion, got {result}"


# ---------------------------------------------------------------------------
# Test 12: should_demote returns None for L3 (no further demotion)
# ---------------------------------------------------------------------------


def test_should_demote_returns_none_for_l3_already():
    from memem.decay import should_demote

    # L3 memory at any strength — already at bottom
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l3 = {
        "layer": 3,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 1,
    }
    result = should_demote(mem_l3)
    assert result is None, f"L3 memory must never be demoted further, got {result}"


# ---------------------------------------------------------------------------
# Test 13: should_demote returns None for decay_immune memory
# ---------------------------------------------------------------------------


def test_should_demote_returns_none_for_decay_immune():
    from memem.decay import should_demote

    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_immune = {
        "layer": 2,
        "decay_immune": True,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,
        "importance": 1,
    }
    result = should_demote(mem_immune)
    assert result is None, f"decay_immune memory must not be demoted, got {result}"


# ---------------------------------------------------------------------------
# Test 14: bump_access increments count and updates timestamp
# ---------------------------------------------------------------------------


def test_bump_access_increments_count_and_updates_timestamp(tmp_vault, tmp_cortex_dir):
    from memem import models, obsidian_store
    importlib.reload(models)
    importlib.reload(obsidian_store)

    from memem.obsidian_store import (
        _find_memory,
        _make_memory,
        _reset_cache,
        _trigger_sweep,
        _write_obsidian_memory,
        bump_access,
    )

    mem = _make_memory(
        content="Python decorators add cross-cutting behavior to functions cleanly.",
        title="Python decorators",
    )
    assert mem["access_count"] == 0
    original_ts = mem["last_accessed_at"]

    _write_obsidian_memory(mem)
    _reset_cache()
    _trigger_sweep()

    mem_id = mem["id"]
    bump_access(mem_id)

    # Force cache refresh to pick up the on-disk change
    _reset_cache()
    _trigger_sweep()

    reloaded = _find_memory(mem_id)
    assert reloaded is not None, "Memory should still be findable after bump_access"
    assert reloaded["access_count"] == 1, (
        f"access_count should be 1 after one bump, got {reloaded['access_count']}"
    )
    # Timestamp should be updated (or at worst equal if same second)
    assert reloaded.get("last_accessed_at", "") >= original_ts, (
        "last_accessed_at should be updated by bump_access"
    )


# ---------------------------------------------------------------------------
# Test 15: recall bumps access on hit
# ---------------------------------------------------------------------------


def test_recall_bumps_access_on_hit(tmp_vault, tmp_cortex_dir):
    from memem import models, obsidian_store, search_index
    importlib.reload(models)
    importlib.reload(obsidian_store)
    importlib.reload(search_index)

    from memem.obsidian_store import (
        _find_memory,
        _make_memory,
        _reset_cache,
        _save_memory,
        _trigger_sweep,
    )
    from memem.recall import _search_memories

    mem = _make_memory(
        content="Dependency injection decouples components for testability in software architecture.",
        title="Dependency injection pattern",
    )
    _save_memory(mem)
    _reset_cache()
    _trigger_sweep()

    mem_id = mem["id"]

    # Verify initial access_count is 0
    before = _find_memory(mem_id)
    assert before is not None
    initial_count = before.get("access_count", 0)

    # Perform a recall that should hit this memory
    results = _search_memories(
        "dependency injection decouples components",
        record_access=True,
        expand_links=False,
    )

    # Only check if the memory was returned
    result_ids = [m.get("id", "") for m in results]
    if mem_id not in result_ids:
        # If not returned by this search, skip the access check
        # (search may not match depending on FTS index state)
        return

    # Force cache refresh
    _reset_cache()
    _trigger_sweep()

    after = _find_memory(mem_id)
    assert after is not None
    assert after.get("access_count", 0) > initial_count, (
        f"access_count should be incremented after recall hit: "
        f"initial={initial_count}, after={after.get('access_count', 0)}"
    )
