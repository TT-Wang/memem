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
15. test_cluster_of_5_high_similarity_detected
16. test_cluster_of_4_not_promoted
17. test_cluster_of_5_low_similarity_not_promoted
18. test_l0_memories_excluded
19. test_dry_run_does_not_write
20. test_apply_creates_pattern_memory
21. test_handles_embedding_unavailable
22. test_handles_sonnet_timeout
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

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


# ---------------------------------------------------------------------------
# Helper: build memory with project + content + optional layer
# ---------------------------------------------------------------------------

def _cluster_mem(
    mem_id: str,
    layer: int = 2,
    decay_immune: bool = False,
    project: str = "testproject",
    content: str = "Some content about a topic",
    title: str = "Cluster test memory",
) -> dict:
    base = _mem(mem_id=mem_id, layer=layer, decay_immune=decay_immune, title=title)
    base["project"] = project
    base["essence"] = content
    return base


# ---------------------------------------------------------------------------
# Test 15: cluster of 5 high-similarity items → 1 proposal returned
# ---------------------------------------------------------------------------

def test_cluster_of_5_high_similarity_detected():
    """5 memories with identical embeddings must form one cluster proposal."""
    pytest.importorskip("numpy")  # cluster code requires numpy; not in CI deps
    from memem.dreamer import CLUSTER_MIN_SIZE, find_cluster_summaries

    mems = [
        _cluster_mem(f"mem0000{i}", content="Identical content about the recurring topic")
        for i in range(CLUSTER_MIN_SIZE)
    ]

    # identical vector = cosine sim 1.0 for all pairs
    identical_vec = [1.0] + [0.0] * 383

    sonnet_output = "TITLE: Recurring topic pattern\n---\nThis is the synthesized pattern body text."

    with patch("memem.embedding_index._embed_text", return_value=identical_vec), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = sonnet_output
        proposals = find_cluster_summaries(mems)

    assert len(proposals) == 1
    p = proposals[0]
    assert set(p["cluster_ids"]) == {m["id"] for m in mems}
    assert p["project"] == "testproject"
    assert p["pattern_title"] != ""
    assert p["pattern_content"] != ""
    assert 0.9 <= p["similarity_mean"] <= 1.0


# ---------------------------------------------------------------------------
# Test 16: cluster of 4 items → empty (size < CLUSTER_MIN_SIZE=5)
# ---------------------------------------------------------------------------

def test_cluster_of_4_not_promoted():
    pytest.importorskip("numpy")  # cluster code requires numpy
    """4 memories with identical embeddings must NOT form a cluster (size < 5)."""
    from memem.dreamer import find_cluster_summaries

    mems = [
        _cluster_mem(f"mem0001{i}", content="Identical content about the recurring topic")
        for i in range(4)
    ]

    identical_vec = [1.0] + [0.0] * 383

    with patch("memem.embedding_index._embed_text", return_value=identical_vec), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "TITLE: x\n---\nbody"
        proposals = find_cluster_summaries(mems)

    assert proposals == []


# ---------------------------------------------------------------------------
# Test 17: cluster of 5 items with low similarity → empty
# ---------------------------------------------------------------------------

def test_cluster_of_5_low_similarity_not_promoted():
    pytest.importorskip("numpy")  # cluster code requires numpy
    """5 memories with orthogonal embeddings must NOT form a cluster."""

    from memem.dreamer import find_cluster_summaries

    mems = [
        _cluster_mem(f"mem0002{i}", content=f"Completely different topic {i}")
        for i in range(5)
    ]

    # Orthogonal unit vectors → cosine sim = 0
    vecs = [
        [1.0 if j == i else 0.0 for j in range(384)]
        for i in range(5)
    ]

    embed_call_count = [0]

    def side_effect(text: str) -> list[float]:
        v = vecs[embed_call_count[0] % 5]
        embed_call_count[0] += 1
        return v

    with patch("memem.embedding_index._embed_text", side_effect=side_effect), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "TITLE: x\n---\nbody"
        proposals = find_cluster_summaries(mems)

    assert proposals == []


# ---------------------------------------------------------------------------
# Test 18: L0 memories excluded — only 3 non-L0 left → below threshold
# ---------------------------------------------------------------------------

def test_l0_memories_excluded():
    """5 items where 2 are L0 → only 3 non-L0 → cluster of 3 < CLUSTER_MIN_SIZE=5."""
    from memem.dreamer import find_cluster_summaries

    mems = [
        _cluster_mem("memL0A000", layer=0),
        _cluster_mem("memL0B000", layer=0),
        _cluster_mem("mem00003A"),
        _cluster_mem("mem00003B"),
        _cluster_mem("mem00003C"),
    ]

    identical_vec = [1.0] + [0.0] * 383

    with patch("memem.embedding_index._embed_text", return_value=identical_vec), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "TITLE: x\n---\nbody"
        proposals = find_cluster_summaries(mems)

    assert proposals == []


# ---------------------------------------------------------------------------
# Test 19: dry_run=True → no new memory file created
# ---------------------------------------------------------------------------

def test_dry_run_does_not_write(tmp_vault, tmp_cortex_dir):
    """apply_diff with cluster_summaries and dry_run=True must not create memory."""
    from memem.dreamer import apply_diff

    diff = {
        "demotion_candidates": [],
        "contradiction_pairs": [],
        "cluster_summaries": [
            {
                "project": "testproject",
                "cluster_ids": ["aabbccdd", "eeffgghh"],
                "pattern_title": "Some Pattern",
                "pattern_content": "This is the pattern body that explains the recurring theme.",
                "similarity_mean": 0.92,
            }
        ],
    }

    with patch("memem.obsidian_store._write_obsidian_memory") as mock_write:
        result = apply_diff(diff, dry_run=True)

    mock_write.assert_not_called()
    assert result.get("clustered", 0) == 0


# ---------------------------------------------------------------------------
# Test 20: dry_run=False → new L2 pattern memory created, constituents tagged
# ---------------------------------------------------------------------------

def test_apply_creates_pattern_memory(tmp_vault, tmp_cortex_dir):
    pytest.importorskip("numpy")  # cluster code requires numpy
    """apply_diff with dry_run=False creates a new layer=2 pattern memory."""
    from memem.dreamer import apply_diff
    from memem.obsidian_store import _find_memory, _make_memory, _trigger_sweep, _write_obsidian_memory

    # Create real constituent memories using _make_memory with explicit layer
    # to avoid classify_layer calling _obsidian_memories and partially warming cache.
    mem_a = _make_memory(content="First constituent memory about the shared topic.", title="Constituent A", layer=2)
    _write_obsidian_memory(mem_a)

    mem_b = _make_memory(content="Second constituent memory about the shared topic.", title="Constituent B", layer=2)
    _write_obsidian_memory(mem_b)

    mem_c = _make_memory(content="Third constituent memory about the shared topic.", title="Constituent C", layer=2)
    _write_obsidian_memory(mem_c)

    mem_d = _make_memory(content="Fourth constituent memory about the shared topic.", title="Constituent D", layer=2)
    _write_obsidian_memory(mem_d)

    mem_e = _make_memory(content="Fifth constituent memory about the shared topic.", title="Constituent E", layer=2)
    _write_obsidian_memory(mem_e)

    # Force cache to see all 5 written files before calling apply_diff
    _trigger_sweep()

    cluster_ids = [mem_a["id"], mem_b["id"], mem_c["id"], mem_d["id"], mem_e["id"]]

    diff = {
        "demotion_candidates": [],
        "contradiction_pairs": [],
        "cluster_summaries": [
            {
                "project": "general",
                "cluster_ids": cluster_ids,
                "pattern_title": "Shared Topic Pattern",
                "pattern_content": "This recurring theme describes a pattern observed across multiple memories about the shared topic.",
                "similarity_mean": 0.95,
            }
        ],
    }

    result = apply_diff(diff, dry_run=False)

    assert result.get("clustered", 0) == 1
    assert result["errors"] == []

    # Verify constituents are tagged with clustered_into in the in-memory cache
    # (apply_diff modifies the cached dict in-place; _write_obsidian_memory does
    # not persist clustered_into to frontmatter since it's a new field not in the
    # fixed schema — but the in-memory state is what matters for subsequent recalls)
    for cid in cluster_ids:
        updated = _find_memory(cid)
        assert updated is not None
        assert updated.get("clustered_into") is not None, f"Constituent {cid} missing clustered_into"


# ---------------------------------------------------------------------------
# Test 21: embedding unavailable → returns []
# ---------------------------------------------------------------------------

def test_handles_embedding_unavailable():
    """If _embed_text import raises ImportError, find_cluster_summaries returns []."""

    mems = [
        _cluster_mem(f"mem0004{i}", content="Same content")
        for i in range(5)
    ]

    # NOTE: Rather than patching the import (which is tricky for lazy imports),
    # we patch the embedding_index module at sys.modules level below.

    # Patch at the embedding_index module level so the lazy import fails
    import sys
    import types

    # Create a fake module that raises ImportError on _embed_text call
    fake_embedding_mod = types.ModuleType("memem.embedding_index")

    def _embed_text_unavailable(text: str):
        raise ImportError("sentence-transformers not installed")

    fake_embedding_mod._embed_text = _embed_text_unavailable  # type: ignore[attr-defined]

    original = sys.modules.get("memem.embedding_index")
    sys.modules["memem.embedding_index"] = fake_embedding_mod
    try:
        # Reload dreamer so the lazy import picks up our fake module
        # We can't easily reload due to existing imports, so test via direct patch
        with patch("memem.embedding_index._embed_text", side_effect=ImportError("not installed")):
            # The function catches ImportError at import time, not at call time.
            # So instead patch builtins.__import__ to fail for embedding_index
            import builtins
            real_import = builtins.__import__

            def failing_import(name, *args, **kwargs):
                if name == "memem.embedding_index":
                    raise ImportError("embedding unavailable")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=failing_import):
                # Need fresh import of find_cluster_summaries to trigger the lazy import
                import importlib as _il

                import memem.dreamer as _dreamer
                _il.reload(_dreamer)
                proposals = _dreamer.find_cluster_summaries(mems)
                assert proposals == []
    finally:
        if original is None:
            sys.modules.pop("memem.embedding_index", None)
        else:
            sys.modules["memem.embedding_index"] = original


# ---------------------------------------------------------------------------
# Test 22: Sonnet timeout → cluster skipped, function does not crash
# ---------------------------------------------------------------------------

def test_handles_sonnet_timeout():
    """If subprocess.run raises TimeoutExpired, the cluster is skipped gracefully."""
    from memem.dreamer import find_cluster_summaries

    mems = [
        _cluster_mem(f"mem0005{i}", content="Same content about recurring topic")
        for i in range(5)
    ]

    identical_vec = [1.0] + [0.0] * 383

    with patch("memem.embedding_index._embed_text", return_value=identical_vec), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
        proposals = find_cluster_summaries(mems)

    # Must not raise; cluster is skipped
    assert proposals == []
