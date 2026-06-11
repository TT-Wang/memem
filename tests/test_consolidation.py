"""Tests for cluster_merge category in memem.dreamer (formerly memem.consolidation).

consolidation.py was deleted in v2.8 — all clustering logic folded into dreamer.py
as the 'cluster_merge' category.  These tests cover:

1. TestClusterMergesIntoCanonical      — cluster of near-identical memories → 1 canonical
2. TestBelowThresholdLeftAlone         — low cosine → no cluster_merge proposals
3. TestSupportingIdsRespected          — B17 fix: only supporting_ids members get invalidated
4. TestProtectedExcluded               — L0 / decay_immune never enter clusters
5. TestMinClusterSizeRespected         — cluster below min_size is skipped
6. TestCanonicalSaveFailNoInvalidation — B17 fix: if canonical save fails, members stay intact
7. TestDryRunNoDiskWrites              — dry_run=True → proposals counted, nothing written
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_memory(content: str, title: str, layer: int = 2, project: str = "general") -> dict:
    """Create (but do not save) a minimal memory dict via _make_memory."""
    from memem.obsidian_store import _make_memory
    return _make_memory(
        content=content,
        title=title,
        project=project,
        source_type="user",
        importance=3,
        layer=layer,
    )


def _save_test_memory(content: str, title: str, layer: int = 2, project: str = "general") -> dict:
    """Create and save a memory to the vault. Returns the saved memory dict."""
    from memem.obsidian_store import _make_memory, _save_memory
    mem = _make_memory(
        content=content,
        title=title,
        project=project,
        source_type="user",
        importance=3,
        layer=layer,
    )
    _save_memory(mem)
    return mem


def _fake_embedding(text: str, seed: float = 0.9) -> list[float]:
    """Unit-length vector; all same-seed vectors have cosine = 1.0 with each other."""
    dim = 384
    v = [seed] * dim
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _fake_embedding_low(text: str) -> list[float]:
    """Orthogonal to _fake_embedding output — cosine ≈ 0."""
    dim = 384
    v = [(1.0 if i % 2 == 0 else -1.0) for i in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _haiku_merge_response(cluster_mems: list[dict], title: str, content: str,
                           contradictions: list[dict] | None = None) -> dict:
    """Build a canned Haiku merge response covering all cluster members."""
    return {
        "canonical_title": title,
        "canonical_content": content,
        "supporting_ids": [m["id"][:8] for m in cluster_mems],
        "contradictions": contradictions or [],
    }


# ---------------------------------------------------------------------------
# Test 1 — cluster of near-identical memories → 1 canonical + supersession
# ---------------------------------------------------------------------------


class TestClusterMergesIntoCanonical:
    """Cluster of near-identical memories → 1 canonical; members bi-temporally invalidated."""

    def test_cluster_merge_proposals_generated(self, tmp_vault, tmp_cortex_dir):
        """find_cluster_merge_proposals returns a proposal for a tight cluster."""
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory(
            "TypeScript should be used over JavaScript for all new projects.",
            "TypeScript over JavaScript preference",
        )
        m2 = _save_test_memory(
            "Use TypeScript instead of JavaScript; it catches type errors early.",
            "Prefer TypeScript to JavaScript",
        )
        m3 = _save_test_memory(
            "All new code should be written in TypeScript, not plain JavaScript.",
            "TypeScript for all new code",
        )

        cluster_mems = [m1, m2, m3]
        haiku_response = _haiku_merge_response(
            cluster_mems,
            title="TypeScript over JavaScript",
            content="Use TypeScript for all new projects. Catches type errors early.",
        )
        fake_run = MagicMock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(haiku_response),
            stderr="",
        ))

        near_vec = _fake_embedding("typescript")

        mock_model = MagicMock()
        mock_model.encode.return_value = [near_vec for _ in cluster_mems]

        with patch.object(dreamer_mod, "_call_haiku_cluster_merge",
                          wraps=lambda mems: haiku_response):
            proposals = dreamer_mod.find_cluster_merge_proposals(
                cluster_mems,
                min_cluster_size=2,
                similarity_threshold=0.85,
            )
            # Need embeddings — mock them indirectly via the subprocess
            # Actually find_cluster_merge_proposals calls _get_model().encode()
            # so mock the model

        # Re-run with model mocked
        with patch("memem.embedding_index._get_model", return_value=mock_model), \
             patch("memem.embedding_index._try_import", return_value=(None, __import__("numpy"))), \
             patch("memem.dreamer.subprocess.run", fake_run):
            proposals = dreamer_mod.find_cluster_merge_proposals(
                cluster_mems,
                min_cluster_size=2,
                similarity_threshold=0.85,
            )

        assert len(proposals) == 1
        p = proposals[0]
        assert p["canonical_title"] == "TypeScript over JavaScript"
        assert set(p["cluster_ids"]) == {m["id"] for m in cluster_mems}
        assert set(p["supporting_ids"]) == {m["id"][:8] for m in cluster_mems}
        assert "kind:consolidated" in p["source_tags"]

    def test_apply_diff_cluster_merge_creates_canonical(self, tmp_vault, tmp_cortex_dir):
        """apply_diff with cluster_merge creates canonical + invalidates supporting members."""
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TypeScript over JavaScript.", "TS pref 1")
        m2 = _save_test_memory("Prefer TypeScript to JavaScript.", "TS pref 2")
        m3 = _save_test_memory("Use TypeScript for new code.", "TS pref 3")

        cluster_mems = [m1, m2, m3]
        diff = {
            "demotion_candidates": [],
            "contradiction_pairs": [],
            "cluster_summaries": [],
            "cluster_merge": [
                {
                    "project": "general",
                    "cluster_ids": [m["id"] for m in cluster_mems],
                    "supporting_ids": [m["id"][:8] for m in cluster_mems],
                    "canonical_title": "TypeScript over JavaScript",
                    "canonical_content": "Use TypeScript for all new projects.",
                    "source_tags": ["kind:consolidated", "consolidated"],
                    "contradictions": [],
                }
            ],
        }

        result = dreamer_mod.apply_diff(diff, dry_run=False)

        assert result["merged"] == 1
        assert result["errors"] == []

        # Canonical exists in vault
        from memem.obsidian_store import _obsidian_memories
        all_mems = _obsidian_memories(include_deprecated=True)
        canonicals = [m for m in all_mems if "kind:consolidated" in (m.get("domain_tags") or [])]
        assert len(canonicals) >= 1

        # Source members should be invalidated (invalid_at set)
        from memem.obsidian_store import _find_memory
        for src in cluster_mems:
            live = _find_memory(src["id"])
            assert live is not None
            assert live.get("invalid_at") is not None, (
                f"Member {src['id'][:8]} should be invalidated after cluster_merge apply"
            )
            assert live.get("replaced_by") is not None


# ---------------------------------------------------------------------------
# Test 2 — below threshold → no cluster_merge proposals
# ---------------------------------------------------------------------------


class TestBelowThresholdLeftAlone:
    """Memories with low cosine similarity must NOT generate cluster_merge proposals."""

    def test_below_threshold_no_proposals(self, tmp_vault, tmp_cortex_dir):
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TypeScript is preferred over JavaScript.", "TypeScript pref")
        m2 = _save_test_memory("Use black for Python formatting.", "Python formatter")
        m3 = _save_test_memory("Always run mypy before committing Python code.", "mypy check")

        mems = [m1, m2, m3]
        # Orthogonal embeddings → cosine = 0 (well below 0.85)
        orthogonal_vecs = [
            [1.0 if j == i else 0.0 for j in range(384)]
            for i in range(3)
        ]

        mock_model = MagicMock()
        mock_model.encode.return_value = orthogonal_vecs

        fake_run = MagicMock()
        with patch("memem.embedding_index._get_model", return_value=mock_model), \
             patch("memem.embedding_index._try_import", return_value=(None, __import__("numpy"))), \
             patch("memem.dreamer.subprocess.run", fake_run):
            proposals = dreamer_mod.find_cluster_merge_proposals(mems, min_cluster_size=2)

        assert proposals == []
        fake_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — B17 fix: supporting_ids respected
# ---------------------------------------------------------------------------


class TestSupportingIdsRespected:
    """B17 fix: only members in supporting_ids get invalidated; others stay active."""

    def test_only_supporting_ids_invalidated(self, tmp_vault, tmp_cortex_dir):
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TS preference v1", "TS v1")
        m2 = _save_test_memory("TS preference v2", "TS v2")
        m3 = _save_test_memory("TS preference v3", "TS v3")

        # Haiku only claims m1 and m2 — m3 NOT in supporting_ids
        diff = {
            "demotion_candidates": [],
            "contradiction_pairs": [],
            "cluster_summaries": [],
            "cluster_merge": [
                {
                    "project": "general",
                    "cluster_ids": [m1["id"], m2["id"], m3["id"]],
                    "supporting_ids": [m1["id"][:8], m2["id"][:8]],  # m3 NOT included
                    "canonical_title": "TS preference merged",
                    "canonical_content": "Use TypeScript for new projects.",
                    "source_tags": ["kind:consolidated", "consolidated"],
                    "contradictions": [],
                }
            ],
        }

        result = dreamer_mod.apply_diff(diff, dry_run=False)

        assert result["merged"] == 1
        assert result["errors"] == []

        from memem.obsidian_store import _find_memory

        # m1 and m2 should be invalidated
        live_m1 = _find_memory(m1["id"])
        assert live_m1 is not None
        assert live_m1.get("invalid_at") is not None, "m1 (in supporting_ids) should be invalidated"

        live_m2 = _find_memory(m2["id"])
        assert live_m2 is not None
        assert live_m2.get("invalid_at") is not None, "m2 (in supporting_ids) should be invalidated"

        # m3 NOT in supporting_ids — must remain active
        live_m3 = _find_memory(m3["id"])
        assert live_m3 is not None
        assert live_m3.get("invalid_at") is None, (
            "m3 not in supporting_ids — must NOT be invalidated (B17 fix)"
        )

    def test_replaced_by_points_to_canonical(self, tmp_vault, tmp_cortex_dir):
        """Each invalidated member must have replaced_by = canonical_id."""
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TypeScript is preferred over JavaScript for all projects.", "TS pref v1")
        m2 = _save_test_memory("Use TypeScript instead of JavaScript for better type safety.", "TS pref v2")

        diff = {
            "demotion_candidates": [],
            "contradiction_pairs": [],
            "cluster_summaries": [],
            "cluster_merge": [
                {
                    "project": "general",
                    "cluster_ids": [m1["id"], m2["id"]],
                    "supporting_ids": [m1["id"][:8], m2["id"][:8]],
                    "canonical_title": "TS canonical",
                    "canonical_content": "TypeScript preference.",
                    "source_tags": ["kind:consolidated", "consolidated"],
                    "contradictions": [],
                }
            ],
        }

        result = dreamer_mod.apply_diff(diff, dry_run=False)
        assert result["merged"] == 1

        from memem.obsidian_store import _find_memory, _obsidian_memories

        # Find canonical
        all_mems = _obsidian_memories(include_deprecated=True)
        canonicals = [m for m in all_mems if "kind:consolidated" in (m.get("domain_tags") or [])]
        assert len(canonicals) >= 1
        canonical_id = canonicals[0]["id"]

        for src in [m1, m2]:
            live = _find_memory(src["id"])
            assert live is not None
            assert live.get("replaced_by") == canonical_id, (
                f"Member {src['id'][:8]}.replaced_by should be {canonical_id[:8]}"
            )


# ---------------------------------------------------------------------------
# Test 4 — protected memories excluded from clusters
# ---------------------------------------------------------------------------


class TestProtectedExcluded:
    """L0 and decay_immune memories must never enter cluster_merge proposals."""

    def test_l0_excluded_from_cluster_merge_proposals(self, tmp_vault, tmp_cortex_dir):
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        l0_mem = _make_test_memory("Core L0 project identity content that must be preserved.", "L0 memory", layer=0)
        normal_mem_a = _make_test_memory("TypeScript is preferred over JavaScript for new code.", "TS pref A")
        normal_mem_b = _make_test_memory("Use TypeScript instead of JavaScript for type safety.", "TS pref B")

        mems = [l0_mem, normal_mem_a, normal_mem_b]
        near_vec = _fake_embedding("typescript")

        mock_model = MagicMock()
        mock_model.encode.return_value = [near_vec for _ in [normal_mem_a, normal_mem_b]]

        haiku_response = {
            "canonical_title": "TS canonical",
            "canonical_content": "TypeScript preference.",
            "supporting_ids": [normal_mem_a["id"][:8], normal_mem_b["id"][:8]],
            "contradictions": [],
        }
        fake_run = MagicMock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(haiku_response),
            stderr="",
        ))

        with patch("memem.embedding_index._get_model", return_value=mock_model), \
             patch("memem.embedding_index._try_import", return_value=(None, __import__("numpy"))), \
             patch("memem.dreamer.subprocess.run", fake_run):
            proposals = dreamer_mod.find_cluster_merge_proposals(mems, min_cluster_size=2)

        # L0 must not appear in any cluster
        for prop in proposals:
            assert l0_mem.get("id", "") not in prop["cluster_ids"], (
                "L0 memory must not be a cluster member"
            )

    def test_decay_immune_excluded_from_cluster_merge_proposals(self):
        """decay_immune memories must never enter cluster_merge proposals."""
        from memem.dreamer import _is_protected

        immune_mem = {
            "id": "immuneid1",
            "layer": 2,
            "decay_immune": True,
            "title": "Decay immune memory",
            "status": "active",
        }
        assert _is_protected(immune_mem) is True


# ---------------------------------------------------------------------------
# Test 5 — min cluster size respected
# ---------------------------------------------------------------------------


class TestMinClusterSizeRespected:
    """Cluster below min_cluster_size must not generate a proposal."""

    def test_single_memory_no_proposal(self):
        """A single-memory 'cluster' must not generate a proposal."""
        from memem.dreamer import find_cluster_merge_proposals

        single_mem = {
            "id": "solo1234",
            "layer": 2,
            "decay_immune": False,
            "title": "Solo memory",
            "essence": "Only one memory here.",
            "status": "active",
            "project": "general",
            "domain_tags": [],
        }

        near_vec = _fake_embedding("solo")
        mock_model = MagicMock()
        mock_model.encode.return_value = [near_vec]

        with patch("memem.embedding_index._get_model", return_value=mock_model), \
             patch("memem.embedding_index._try_import", return_value=(None, __import__("numpy"))):
            proposals = find_cluster_merge_proposals([single_mem], min_cluster_size=2)

        assert proposals == []


# ---------------------------------------------------------------------------
# Test 6 — B17 canonical save failure → members NOT invalidated
# ---------------------------------------------------------------------------


class TestCanonicalSaveFailNoInvalidation:
    """If canonical save fails, B17 requires that no members are invalidated."""

    def test_members_untouched_when_canonical_save_fails(self, tmp_vault, tmp_cortex_dir):
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TypeScript is preferred over JavaScript for new projects.", "TS pref A")
        m2 = _save_test_memory("Use TypeScript instead of JavaScript for all new code.", "TS pref B")

        diff = {
            "demotion_candidates": [],
            "contradiction_pairs": [],
            "cluster_summaries": [],
            "cluster_merge": [
                {
                    "project": "general",
                    "cluster_ids": [m1["id"], m2["id"]],
                    "supporting_ids": [m1["id"][:8], m2["id"][:8]],
                    "canonical_title": "TS canonical",
                    "canonical_content": "Use TypeScript for all projects. Catches type errors early.",
                    "source_tags": ["kind:consolidated", "consolidated"],
                    "contradictions": [],
                }
            ],
        }

        # Make _save_memory raise so canonical save fails
        with patch("memem.obsidian_store._save_memory", side_effect=RuntimeError("disk full")):
            result = dreamer_mod.apply_diff(diff, dry_run=False)

        # Expect error recorded, merged=0 (no successful merge)
        assert result["merged"] == 0
        assert any("canonical save failed" in e for e in result["errors"])

        # Members must be untouched (not invalidated)
        from memem.obsidian_store import _find_memory
        for src in [m1, m2]:
            live = _find_memory(src["id"])
            if live:
                assert live.get("invalid_at") is None, (
                    f"Member {src['id'][:8]} must not be invalidated when canonical save failed"
                )


# ---------------------------------------------------------------------------
# Test 7 — dry_run=True → proposals counted, nothing written
# ---------------------------------------------------------------------------


class TestDryRunNoDiskWrites:
    """dry_run=True → merged count returned, no disk writes."""

    def test_dry_run_cluster_merge_no_writes(self, tmp_vault, tmp_cortex_dir):
        import importlib

        import memem.dreamer as dreamer_mod
        import memem.obsidian_store as obs
        importlib.reload(obs)
        importlib.reload(dreamer_mod)

        m1 = _save_test_memory("TypeScript over JavaScript.", "TS pref 1")
        m2 = _save_test_memory("Prefer TypeScript to JavaScript.", "TS pref 2")
        m3 = _save_test_memory("Use TypeScript for new code.", "TS pref 3")

        from memem.models import OBSIDIAN_MEMORIES_DIR
        files_before = set(OBSIDIAN_MEMORIES_DIR.glob("*.md"))

        diff = {
            "demotion_candidates": [],
            "contradiction_pairs": [],
            "cluster_summaries": [],
            "cluster_merge": [
                {
                    "project": "general",
                    "cluster_ids": [m1["id"], m2["id"], m3["id"]],
                    "supporting_ids": [m1["id"][:8], m2["id"][:8], m3["id"][:8]],
                    "canonical_title": "TypeScript preference",
                    "canonical_content": "Use TypeScript for all new projects.",
                    "source_tags": ["kind:consolidated", "consolidated"],
                    "contradictions": [],
                }
            ],
        }

        result = dreamer_mod.apply_diff(diff, dry_run=True)

        files_after = set(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
        new_files = files_after - files_before

        # Count is populated but nothing written
        assert result["merged"] == 1
        assert new_files == set(), f"Unexpected new files in dry_run: {new_files}"

        # Source memories must remain active
        from memem.obsidian_store import _find_memory
        for src in [m1, m2, m3]:
            live = _find_memory(src["id"])
            if live:
                assert live.get("invalid_at") is None, (
                    f"dry_run should not invalidate {src['id'][:8]}"
                )
