"""Tests for damped decay with L0 pin and decay_immune flag (memem v2 m3).

Covers:
1.  compute_strength: fresh memory high
2.  compute_strength: old unaccessed memory low
3.  compute_strength: recent access boosts (via telemetry sidecar)
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
15. recall does NOT rewrite memory files (mtime unchanged), but sidecar IS updated
16. compute_strength reads access_count from sidecar, not frontmatter
17. compute_strength reads last_accessed from sidecar with frontmatter fallback
"""
from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Test 1: fresh memory → high strength
# ---------------------------------------------------------------------------


def test_compute_strength_fresh_memory_high(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import compute_strength

    mem = {
        "id": "abc12345",
        "created_at": datetime.now(UTC).isoformat(),
        "last_accessed_at": datetime.now(UTC).isoformat(),
        "importance": 3,
        "layer": 2,
    }
    strength = compute_strength(mem)
    # Fresh memory with default importance weight (0.6 + 0.2*(3-1) = 1.0) should be ≥ 0.5
    assert strength >= 0.5, f"Fresh memory strength should be ≥ 0.5, got {strength}"


# ---------------------------------------------------------------------------
# Test 2: old unaccessed memory → low strength
# ---------------------------------------------------------------------------


def test_compute_strength_old_unaccessed_low(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import compute_strength

    old_ts = (datetime.now(UTC) - timedelta(days=180)).isoformat()
    mem = {
        "id": "abc12345",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 3,
        "layer": 2,
    }
    strength = compute_strength(mem)
    assert strength < 0.2, f"180-day-old unaccessed memory strength should be < 0.2, got {strength}"


# ---------------------------------------------------------------------------
# Test 3: recent access boosts strength vs. untouched (via telemetry sidecar)
# ---------------------------------------------------------------------------


def test_compute_strength_recent_access_boosts(monkeypatch):
    import memem.decay as decay_mod
    from memem.decay import compute_strength

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=30)).isoformat()
    now_ts = now.isoformat()

    # Boosted: 10 accesses in sidecar, sidecar last_accessed = now
    def tel_boosted(mem_id):
        return {"access_count": 10, "last_accessed": now_ts}

    # Untouched: 0 accesses in sidecar, no sidecar last_accessed
    def tel_untouched(mem_id):
        return {"access_count": 0, "last_accessed": ""}

    boosted = {
        "id": "boosted01",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 3,
        "layer": 2,
    }
    untouched = {
        "id": "untch0001",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 3,
        "layer": 2,
    }

    monkeypatch.setattr(decay_mod, "_get_telemetry", tel_boosted)
    boosted_strength = compute_strength(boosted, now=now)

    monkeypatch.setattr(decay_mod, "_get_telemetry", tel_untouched)
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


def test_should_demote_returns_none_for_l0(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import should_demote

    # L0 memory with very old timestamp (effectively zero strength)
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l0 = {
        "id": "l0mem0001",
        "layer": 0,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 1,
    }
    result = should_demote(mem_l0)
    assert result is None, f"L0 memory must never be demoted, got {result}"


# ---------------------------------------------------------------------------
# Test 11: should_demote returns next layer for weak non-immune memory
# ---------------------------------------------------------------------------


def test_should_demote_returns_next_layer_for_weak_memory(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import should_demote

    # L2 memory with extremely old access → very weak
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l2 = {
        "id": "l2mem0001",
        "layer": 2,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 1,
    }
    result = should_demote(mem_l2)
    assert result == 3, f"Weak L2 memory should be suggested for L3 demotion, got {result}"


# ---------------------------------------------------------------------------
# Test 12: should_demote returns None for L3 (no further demotion)
# ---------------------------------------------------------------------------


def test_should_demote_returns_none_for_l3_already(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import should_demote

    # L3 memory at any strength — already at bottom
    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_l3 = {
        "id": "l3mem0001",
        "layer": 3,
        "decay_immune": False,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 1,
    }
    result = should_demote(mem_l3)
    assert result is None, f"L3 memory must never be demoted further, got {result}"


# ---------------------------------------------------------------------------
# Test 13: should_demote returns None for decay_immune memory
# ---------------------------------------------------------------------------


def test_should_demote_returns_none_for_decay_immune(monkeypatch):
    import memem.decay as decay_mod
    monkeypatch.setattr(decay_mod, "_get_telemetry", lambda mem_id: {"access_count": 0, "last_accessed": ""})
    from memem.decay import should_demote

    old_ts = (datetime.now(UTC) - timedelta(days=9999)).isoformat()
    mem_immune = {
        "id": "immumem01",
        "layer": 2,
        "decay_immune": True,
        "created_at": old_ts,
        "last_accessed_at": old_ts,
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
# Test 15: recall does NOT rewrite memory files (mtime unchanged), but
#          telemetry sidecar IS updated (regression: B8 / B12)
# ---------------------------------------------------------------------------


def test_recall_does_not_rewrite_memory_files(tmp_vault, tmp_cortex_dir):
    """Assert that _search_memories with record_access=True does NOT modify
    the memory markdown file (mtime must not change) while the telemetry
    sidecar does get an access count increment.
    """
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
    from memem.telemetry import _get_telemetry

    mem = _make_memory(
        content="Memoization caches function results to avoid redundant computation.",
        title="Memoization pattern",
    )
    _save_memory(mem)
    _reset_cache()
    _trigger_sweep()

    mem_id = mem["id"]

    # Find the on-disk file and record its mtime
    reloaded = _find_memory(mem_id)
    assert reloaded is not None, "Memory must be findable before recall"

    # Find the vault path for this memory file
    from memem.models import OBSIDIAN_MEMORIES_DIR
    memory_files = list(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
    # Filter to find this specific memory
    mem_file = None
    for f in memory_files:
        content = f.read_text()
        if mem_id[:8] in f.name or mem_id[:8] in content or mem_id in content:
            mem_file = f
            break

    assert mem_file is not None, f"Could not find on-disk file for memory {mem_id[:8]}"
    mtime_before = os.path.getmtime(mem_file)

    # Telemetry count before recall
    tel_before = _get_telemetry(mem_id)
    count_before = tel_before.get("access_count", 0)

    # Perform a recall with record_access=True
    results = _search_memories(
        "memoization caches function results",
        record_access=True,
        expand_links=False,
    )

    result_ids = [m.get("id", "") for m in results]
    if mem_id not in result_ids:
        # FTS didn't surface the memory in this environment — exercise the
        # access-recording path directly so the assertions below still run
        # unconditionally (previously this test silently passed by returning
        # here without verifying anything).
        from memem.telemetry import _record_access
        _record_access(mem_id)

    # File mtime must NOT have changed — neither recall nor direct
    # _record_access may rewrite the markdown file.
    mtime_after = os.path.getmtime(mem_file)
    assert mtime_after == mtime_before, (
        f"Memory file mtime changed after recall — bump_access must NOT be called on recall path. "
        f"before={mtime_before}, after={mtime_after}"
    )

    # Telemetry sidecar access_count MUST have increased
    tel_after = _get_telemetry(mem_id)
    count_after = tel_after.get("access_count", 0)
    assert count_after > count_before, (
        f"Telemetry sidecar access_count should increase on recall: "
        f"before={count_before}, after={count_after}"
    )


# ---------------------------------------------------------------------------
# Test 16: compute_strength reads access_count from sidecar, not frontmatter
# ---------------------------------------------------------------------------


def test_compute_strength_uses_sidecar_access_count_not_frontmatter(monkeypatch):
    """Verify that access_count in frontmatter is ignored; sidecar value drives strength."""
    import memem.decay as decay_mod
    from memem.decay import compute_strength

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=30)).isoformat()
    now_ts = now.isoformat()

    # Frontmatter says 0 accesses; sidecar says 20 accesses, last_accessed = now
    # High sidecar count should yield significantly higher strength than 0
    def tel_with_accesses(mem_id):
        return {"access_count": 20, "last_accessed": now_ts}

    def tel_zero(mem_id):
        return {"access_count": 0, "last_accessed": ""}

    mem_frontmatter_zero = {
        "id": "fmz00001",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 0,  # frontmatter — should be ignored
        "importance": 3,
        "layer": 2,
    }

    mem_frontmatter_high = {
        "id": "fmh00001",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "access_count": 1000,  # frontmatter high — should still be ignored
        "importance": 3,
        "layer": 2,
    }

    monkeypatch.setattr(decay_mod, "_get_telemetry", tel_with_accesses)
    strength_sidecar_high = compute_strength(mem_frontmatter_zero, now=now)

    monkeypatch.setattr(decay_mod, "_get_telemetry", tel_zero)
    strength_sidecar_zero = compute_strength(mem_frontmatter_high, now=now)

    # Sidecar-high should dominate, sidecar-zero should be weak (30-day-old no access)
    assert strength_sidecar_high > strength_sidecar_zero, (
        f"Sidecar access_count=20 ({strength_sidecar_high:.3f}) should beat "
        f"sidecar=0 ({strength_sidecar_zero:.3f}) regardless of frontmatter values"
    )


# ---------------------------------------------------------------------------
# Test 17: compute_strength uses sidecar last_accessed with frontmatter fallback
# ---------------------------------------------------------------------------


def test_compute_strength_last_accessed_fallback(monkeypatch):
    """Sidecar last_accessed takes priority; frontmatter last_accessed_at is fallback."""
    import memem.decay as decay_mod
    from memem.decay import compute_strength

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=90)).isoformat()
    new_ts = (now - timedelta(hours=1)).isoformat()

    # Sidecar has recent timestamp; frontmatter has old timestamp
    monkeypatch.setattr(
        decay_mod,
        "_get_telemetry",
        lambda mem_id: {"access_count": 0, "last_accessed": new_ts},
    )
    mem_sidecar_recent = {
        "id": "sir00001",
        "created_at": old_ts,
        "last_accessed_at": old_ts,  # old frontmatter should be ignored
        "importance": 3,
        "layer": 2,
    }
    strength_sidecar_recent = compute_strength(mem_sidecar_recent, now=now)

    # No sidecar timestamp; frontmatter has recent timestamp
    monkeypatch.setattr(
        decay_mod,
        "_get_telemetry",
        lambda mem_id: {"access_count": 0, "last_accessed": ""},
    )
    mem_frontmatter_recent = {
        "id": "fmr00001",
        "created_at": old_ts,
        "last_accessed_at": new_ts,  # should be used as fallback
        "importance": 3,
        "layer": 2,
    }
    strength_frontmatter_recent = compute_strength(mem_frontmatter_recent, now=now)

    # Both should be high (recent access) and much stronger than a 90-day-old memory
    monkeypatch.setattr(
        decay_mod,
        "_get_telemetry",
        lambda mem_id: {"access_count": 0, "last_accessed": ""},
    )
    mem_old = {
        "id": "old00001",
        "created_at": old_ts,
        "last_accessed_at": old_ts,
        "importance": 3,
        "layer": 2,
    }
    strength_old = compute_strength(mem_old, now=now)

    assert strength_sidecar_recent > strength_old, (
        f"Sidecar-recent ({strength_sidecar_recent:.3f}) should beat old ({strength_old:.3f})"
    )
    assert strength_frontmatter_recent > strength_old, (
        f"Frontmatter-fallback-recent ({strength_frontmatter_recent:.3f}) should beat old ({strength_old:.3f})"
    )
