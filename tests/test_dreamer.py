"""Tests for async offline consolidation pass — memem v2 m4 (dreamer).

Covers:
1.  test_is_protected_l0_returns_true
2.  test_is_protected_decay_immune_returns_true
3.  test_is_protected_normal_l2_returns_false
4.  test_find_demotion_candidates_skips_protected
5.  test_find_demotion_candidates_skips_high_attribution
6.  test_find_contradiction_pairs_returns_unresolved
7.  test_find_contradiction_pairs_skips_already_invalidated
8.  test_find_contradiction_pairs_skips_protected
9.  test_build_diff_returns_expected_keys
10. test_apply_diff_dry_run_no_writes
11. test_apply_diff_actually_demotes_when_apply_true
12. test_apply_diff_protected_memories_not_demoted
13. test_write_diff_log_creates_file_in_dreams_dir
14. test_run_dream_cycle_default_is_dry_run
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helper: build minimal memory dicts
# ---------------------------------------------------------------------------

def _mem(
    mem_id: str = "aabbccdd",
    layer: int = 2,
    decay_immune: bool = False,
    invalid_at: str | None = None,
    contradicts: list[str] | None = None,
    title: str = "Test memory",
    importance: int = 1,
    access_count: int = 0,
    created_at: str | None = None,
) -> dict:
    """Return a minimal memory dict for testing."""
    if created_at is None:
        # Old memory so decay kicks in
        created_at = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    mem: dict = {
        "id": mem_id,
        "title": title,
        "layer": layer,
        "decay_immune": decay_immune,
        "invalid_at": invalid_at,
        "contradicts": contradicts or [],
        "importance": importance,
        "access_count": access_count,
        "created_at": created_at,
        "last_accessed_at": created_at,
        "status": "active",
    }
    return mem


# ---------------------------------------------------------------------------
# Test 1: _is_protected — L0 → True
# ---------------------------------------------------------------------------

def test_is_protected_l0_returns_true():
    from memem.dreamer import _is_protected

    mem = _mem(layer=0)
    assert _is_protected(mem) is True


# ---------------------------------------------------------------------------
# Test 2: _is_protected — decay_immune=True on L2 → True
# ---------------------------------------------------------------------------

def test_is_protected_decay_immune_returns_true():
    from memem.dreamer import _is_protected

    mem = _mem(layer=2, decay_immune=True)
    assert _is_protected(mem) is True


# ---------------------------------------------------------------------------
# Test 3: _is_protected — normal L2, not immune → False
# ---------------------------------------------------------------------------

def test_is_protected_normal_l2_returns_false():
    from memem.dreamer import _is_protected

    mem = _mem(layer=2, decay_immune=False)
    assert _is_protected(mem) is False


# ---------------------------------------------------------------------------
# Test 4: find_demotion_candidates skips L0 even when weak
# ---------------------------------------------------------------------------

def test_find_demotion_candidates_skips_protected():
    """L0 memories must never appear in demotion candidates even when weak."""
    from memem.dreamer import find_demotion_candidates

    l0_mem = _mem(mem_id="l0aaaaaa", layer=0, importance=1)
    l1_mem = _mem(mem_id="l1aaaaaa", layer=1, importance=1)

    # Both are very old (low strength), no attribution events
    with patch("memem.dreamer._recent_attribution", return_value=None):
        candidates = find_demotion_candidates([l0_mem, l1_mem])

    ids = [c["memory_id"] for c in candidates]
    assert "l0aaaaaa" not in ids, "L0 must never be a demotion candidate"
    # L1 may or may not be suggested depending on should_demote; we just need L0 skipped


# ---------------------------------------------------------------------------
# Test 5: find_demotion_candidates skips memory with high attribution
# ---------------------------------------------------------------------------

def test_find_demotion_candidates_skips_high_attribution():
    """A weak memory with recent attribution >= threshold should NOT be demoted."""
    from memem.dreamer import LOW_ATTRIBUTION_THRESHOLD, find_demotion_candidates

    weak_mem = _mem(mem_id="weakmem1", layer=2, importance=1)

    # Attribution is above threshold → should NOT be a candidate
    high_attr = LOW_ATTRIBUTION_THRESHOLD + 0.1
    with patch("memem.dreamer._recent_attribution", return_value=high_attr):
        candidates = find_demotion_candidates([weak_mem])

    ids = [c["memory_id"] for c in candidates]
    assert "weakmem1" not in ids, "Memory with high attribution should not be demoted"


# ---------------------------------------------------------------------------
# Test 6: find_contradiction_pairs — unresolved pair returned
# ---------------------------------------------------------------------------

def test_find_contradiction_pairs_returns_unresolved():
    from memem.dreamer import find_contradiction_pairs

    mem_a = _mem(mem_id="aaaaaaaa", contradicts=["bbbbbbbb"])
    mem_b = _mem(mem_id="bbbbbbbb")

    pairs = find_contradiction_pairs([mem_a, mem_b])
    assert len(pairs) == 1
    assert pairs[0]["memory_a"] == "aaaaaaaa"
    assert pairs[0]["memory_b"] == "bbbbbbbb"


# ---------------------------------------------------------------------------
# Test 7: find_contradiction_pairs — skips if A already invalidated
# ---------------------------------------------------------------------------

def test_find_contradiction_pairs_skips_already_invalidated():
    from memem.dreamer import find_contradiction_pairs

    mem_a = _mem(mem_id="aaaaaaaa", contradicts=["bbbbbbbb"], invalid_at="2025-01-01T00:00:00Z")
    mem_b = _mem(mem_id="bbbbbbbb")

    pairs = find_contradiction_pairs([mem_a, mem_b])
    assert pairs == [], "Invalidated memory A should produce no contradiction pair"


# ---------------------------------------------------------------------------
# Test 8: find_contradiction_pairs — skips pair if one is L0 (protected)
# ---------------------------------------------------------------------------

def test_find_contradiction_pairs_skips_protected():
    from memem.dreamer import find_contradiction_pairs

    # mem_a contradicts an L0 memory — the L0 is protected → pair skipped
    mem_a = _mem(mem_id="aaaaaaaa", contradicts=["l0000000"])
    mem_l0 = _mem(mem_id="l0000000", layer=0)

    pairs = find_contradiction_pairs([mem_a, mem_l0])
    assert pairs == [], "Contradiction pair involving L0 must be skipped"


# ---------------------------------------------------------------------------
# Test 9: build_diff — returns all expected top-level keys
# ---------------------------------------------------------------------------

def test_build_diff_returns_expected_keys():
    from memem.dreamer import build_diff

    diff = build_diff([])
    assert "generated_at" in diff
    assert "vault_size" in diff
    assert "demotion_candidates" in diff
    assert "contradiction_pairs" in diff
    assert "cluster_summaries" in diff
    assert "stats" in diff


# ---------------------------------------------------------------------------
# Test 10: apply_diff dry_run=True — no files mutated
# ---------------------------------------------------------------------------

def test_apply_diff_dry_run_no_writes():
    """apply_diff with dry_run=True must not call _write_obsidian_memory."""
    from memem.dreamer import apply_diff

    diff = {
        "demotion_candidates": [
            {"memory_id": "aabbccdd", "suggested_layer": 3}
        ],
        "contradiction_pairs": [],
    }

    with patch("memem.obsidian_store._write_obsidian_memory") as mock_write:
        result = apply_diff(diff, dry_run=True)

    mock_write.assert_not_called()
    assert result["demoted"] == 1  # counted but not written
    assert result["invalidated"] == 0


# ---------------------------------------------------------------------------
# Test 11: apply_diff actually demotes when dry_run=False
# ---------------------------------------------------------------------------

def test_apply_diff_actually_demotes_when_apply_true(tmp_vault, tmp_cortex_dir):
    """apply_diff with dry_run=False writes the layer change to disk."""
    from memem.dreamer import apply_diff
    from memem.obsidian_store import _find_memory, _make_memory, _write_obsidian_memory

    # Create a real L2 memory in the vault
    mem = _make_memory(content="Some old forgotten thing.", title="Old forgotten memory")
    mem["layer"] = 2
    _write_obsidian_memory(mem)
    mem_id = mem["id"]

    diff = {
        "demotion_candidates": [
            {"memory_id": mem_id, "suggested_layer": 3}
        ],
        "contradiction_pairs": [],
    }

    result = apply_diff(diff, dry_run=False)

    assert result["demoted"] == 1
    assert result["errors"] == []

    # Verify the layer was actually changed on disk
    updated = _find_memory(mem_id)
    assert updated is not None
    assert int(updated["layer"]) == 3


# ---------------------------------------------------------------------------
# Test 12: apply_diff — protected memories not demoted even if in diff
# ---------------------------------------------------------------------------

def test_apply_diff_protected_memories_not_demoted(tmp_vault, tmp_cortex_dir):
    """If an L0 sneaks into demotion_candidates, apply must refuse to demote it."""
    from memem.dreamer import apply_diff
    from memem.obsidian_store import _find_memory, _make_memory, _write_obsidian_memory

    # Create a real L0 memory
    mem = _make_memory(content="Core always-loaded fact.", title="L0 anchor memory")
    mem["layer"] = 0
    _write_obsidian_memory(mem)
    mem_id = mem["id"]

    # Force the L0 into the diff (bypassing find_demotion_candidates safety)
    diff = {
        "demotion_candidates": [
            {"memory_id": mem_id, "suggested_layer": 1}
        ],
        "contradiction_pairs": [],
    }

    result = apply_diff(diff, dry_run=False)

    # Should be refused — error logged, nothing demoted
    assert result["demoted"] == 0
    assert any("protected" in e for e in result["errors"])

    # L0 must still be layer 0 on disk
    still_l0 = _find_memory(mem_id)
    assert still_l0 is not None
    assert int(still_l0["layer"]) == 0


# ---------------------------------------------------------------------------
# Test 13: write_diff_log — creates file in dreams dir with valid JSON
# ---------------------------------------------------------------------------

def test_write_diff_log_creates_file_in_dreams_dir(tmp_cortex_dir):
    """write_diff_log writes a valid JSON file under MEMEM_DIR/dreams/."""
    import importlib

    import memem.dreamer as dreamer_mod

    # Reload dreamer so DREAMS_DIR uses the tmp_cortex_dir
    importlib.reload(dreamer_mod)

    diff = {
        "generated_at": "2026-01-01T00:00:00Z",
        "vault_size": 42,
        "demotion_candidates": [],
        "contradiction_pairs": [],
        "cluster_summaries": [],
        "stats": {"l0_count": 0, "decay_immune_count": 0},
    }

    path = dreamer_mod.write_diff_log(diff)

    assert path.exists(), f"Dream log not created at {path}"
    assert path.suffix == ".json"
    assert path.parent.name == "dreams"

    loaded = json.loads(path.read_text())
    assert loaded["vault_size"] == 42
    assert "demotion_candidates" in loaded


# ---------------------------------------------------------------------------
# Test 14: run_dream_cycle default is dry_run=True
# ---------------------------------------------------------------------------

def test_run_dream_cycle_default_is_dry_run(tmp_vault, tmp_cortex_dir):
    """run_dream_cycle() with no args must return dry_run=True in result."""
    import importlib

    import memem.dreamer as dreamer_mod
    importlib.reload(dreamer_mod)

    result = dreamer_mod.run_dream_cycle()  # no args → dry_run=True
    assert result["dry_run"] is True
    assert "diff_path" in result
    assert "diff" in result
    assert "apply_result" in result
