"""Tests for memem.consolidation — episodic consolidation + contradiction detection."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_memory(content: str, title: str, layer: int = 2, project: str = "general") -> dict:
    """Create and return an in-memory dict (not saved to vault)."""
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
    """Return a unit-length fake embedding vector based on seed value.
    All vectors from the same seed are identical (cosine = 1.0 with each other).
    """
    dim = 384
    v = [seed] * dim
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _fake_embedding_low(text: str) -> list[float]:
    """Return a unit-length embedding that is orthogonal to _fake_embedding's output."""
    dim = 384
    # Alternate signs so dot product with all-same-sign vector ≈ 0
    v = [(1.0 if i % 2 == 0 else -1.0) for i in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClusterMergesIntoCanonical:
    """Cluster of near-identical memories → 1 canonical + supersession markers."""

    def test_cluster_merges_into_canonical_with_supersession(
        self, tmp_vault, tmp_cortex_dir
    ):
        from memem import consolidation, obsidian_store
        importlib.reload(obsidian_store)
        importlib.reload(consolidation)

        # Save 3 memories about the same topic
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

        ids = [m1["id"], m2["id"], m3["id"]]
        near_vec = _fake_embedding("typescript")

        # Mock embeddings — all 3 return the same near-identical vector
        def _mock_compute(memories):
            vecs = [near_vec for _ in memories]
            return list(memories), vecs

        # Mock Haiku call
        haiku_response = {
            "canonical_title": "TypeScript over JavaScript preference",
            "canonical_content": (
                "Use TypeScript instead of JavaScript for all new projects. "
                "TypeScript catches type errors early and is preferred for all new code."
            ),
            "supporting_ids": [m["id"][:8] for m in [m1, m2, m3]],
            "contradictions": [],
        }
        fake_run = MagicMock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(haiku_response),
            stderr="",
        ))

        with patch.object(consolidation, "_compute_embeddings", _mock_compute):
            with patch("memem.consolidation.subprocess.run", fake_run):
                result = consolidation.run_consolidation_pass(
                    layer=2,
                    min_cluster_size=3,
                    similarity_threshold=0.85,
                    dry_run=False,
                )

        assert result.clusters_processed == 1
        assert result.memories_consolidated == 3
        assert len(result.canonical_memories_created) == 1
        assert set(result.superseded_memories) == set(ids)
        assert not result.errors

        # Verify canonical memory is in the vault
        from memem.obsidian_store import _find_memory
        canon_id = result.canonical_memories_created[0]
        canon = _find_memory(canon_id)
        assert canon is not None
        assert "kind:consolidated" in canon.get("domain_tags", [])

        # Verify source memories are marked deprecated/superseded
        for src_id in ids:
            src = _find_memory(src_id)
            assert src is not None
            assert src.get("superseded_by") == canon_id or src.get("status") == "deprecated"


class TestBelowThresholdLeftAlone:
    """Memories with low cosine (~0.5) should NOT be merged."""

    def test_below_threshold_cluster_left_alone(self, tmp_vault, tmp_cortex_dir):
        from memem import consolidation, obsidian_store
        importlib.reload(obsidian_store)
        importlib.reload(consolidation)

        m1 = _save_test_memory("TypeScript is preferred over JavaScript.", "TypeScript pref")
        m2 = _save_test_memory("Use black for Python formatting.", "Python formatter")
        m3 = _save_test_memory("Always run mypy before committing Python code.", "mypy check")

        # Give all 3 different embeddings so cosine < 0.85
        low_vecs: dict[str, list[float]] = {}

        def _mock_compute(memories: list[dict]):
            # Assign orthogonal-ish vectors to each memory
            vecs = []
            for i, mem in enumerate(memories):
                dim = 384
                v = [(1.0 if j == i else 0.0) for j in range(dim)]
                norm = sum(x * x for x in v) ** 0.5 or 1.0
                vecs.append([x / norm for x in v])
            return list(memories), vecs

        fake_run = MagicMock()

        with patch.object(consolidation, "_compute_embeddings", _mock_compute):
            with patch("memem.consolidation.subprocess.run", fake_run):
                result = consolidation.run_consolidation_pass(
                    layer=2,
                    min_cluster_size=3,
                    similarity_threshold=0.85,
                    dry_run=False,
                )

        # No cluster should be formed — cosine between orthogonal unit vecs = 0
        assert result.clusters_processed == 0
        assert result.memories_consolidated == 0
        assert result.canonical_memories_created == []
        # Haiku should NOT have been called
        fake_run.assert_not_called()


class TestContradictionSurfacesAsFlagMemory:
    """Haiku response with contradictions → contradiction-flag memory created."""

    def test_contradiction_surfaces_as_flag_memory(self, tmp_vault, tmp_cortex_dir):
        from memem import consolidation, obsidian_store
        importlib.reload(obsidian_store)
        importlib.reload(consolidation)

        m1 = _save_test_memory("Use PostgreSQL for the main database.", "DB choice: Postgres")
        m2 = _save_test_memory("Use MySQL for the main database instead.", "DB choice: MySQL")
        m3 = _save_test_memory("The main database is relational, use SQL.", "DB is relational")

        near_vec = _fake_embedding("database choice")

        def _mock_compute(memories):
            return list(memories), [near_vec for _ in memories]

        haiku_response = {
            "canonical_title": "Database choice",
            "canonical_content": "The database choice is PostgreSQL or MySQL (conflicting preferences).",
            "supporting_ids": [m1["id"][:8], m2["id"][:8], m3["id"][:8]],
            "contradictions": [
                {
                    "memory_a": m1["id"][:8],
                    "memory_b": m2["id"][:8],
                    "conflict": "Memory A says PostgreSQL, memory B says MySQL.",
                }
            ],
        }
        fake_run = MagicMock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(haiku_response),
            stderr="",
        ))

        with patch.object(consolidation, "_compute_embeddings", _mock_compute):
            with patch("memem.consolidation.subprocess.run", fake_run):
                result = consolidation.run_consolidation_pass(
                    layer=2,
                    min_cluster_size=3,
                    similarity_threshold=0.85,
                    dry_run=False,
                )

        assert result.contradictions_flagged == 1
        assert result.clusters_processed == 1

        # Verify contradiction-flag memory exists in vault
        from memem.obsidian_store import _obsidian_memories
        all_mems = _obsidian_memories(include_deprecated=True)
        flag_mems = [
            m for m in all_mems
            if "kind:contradiction-flag" in m.get("domain_tags", [])
        ]
        assert len(flag_mems) == 1
        assert "contradiction" in flag_mems[0].get("domain_tags", [])
        assert "pending-review" in flag_mems[0].get("domain_tags", [])
        assert "PostgreSQL" in flag_mems[0].get("essence", "") or "contradiction" in flag_mems[0].get("essence", "").lower()


class TestDryRunNoDiskWrites:
    """dry_run=True → result populated but no new files in vault."""

    def test_dry_run_no_disk_writes(self, tmp_vault, tmp_cortex_dir):
        from memem import consolidation, obsidian_store
        importlib.reload(obsidian_store)
        importlib.reload(consolidation)

        m1 = _save_test_memory("TypeScript over JavaScript.", "TS pref 1")
        m2 = _save_test_memory("Prefer TypeScript to JavaScript.", "TS pref 2")
        m3 = _save_test_memory("Use TypeScript for new code.", "TS pref 3")

        from memem.models import OBSIDIAN_MEMORIES_DIR
        files_before = set(OBSIDIAN_MEMORIES_DIR.glob("*.md"))

        near_vec = _fake_embedding("typescript")

        def _mock_compute(memories):
            return list(memories), [near_vec for _ in memories]

        haiku_response = {
            "canonical_title": "TypeScript preference",
            "canonical_content": "Use TypeScript for all new projects.",
            "supporting_ids": [m1["id"][:8], m2["id"][:8], m3["id"][:8]],
            "contradictions": [],
        }
        fake_run = MagicMock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(haiku_response),
            stderr="",
        ))

        with patch.object(consolidation, "_compute_embeddings", _mock_compute):
            with patch("memem.consolidation.subprocess.run", fake_run):
                result = consolidation.run_consolidation_pass(
                    layer=2,
                    min_cluster_size=3,
                    similarity_threshold=0.85,
                    dry_run=True,
                )

        files_after = set(OBSIDIAN_MEMORIES_DIR.glob("*.md"))
        new_files = files_after - files_before

        # Result is populated
        assert result.clusters_processed == 1
        assert result.memories_consolidated == 3
        assert len(result.canonical_memories_created) == 1

        # But no new files were written
        assert new_files == set(), f"Unexpected new files in dry_run: {new_files}"

        # Source memories are NOT marked deprecated (no disk write)
        from memem.obsidian_store import _find_memory
        for src_mem in [m1, m2, m3]:
            live = _find_memory(src_mem["id"])
            if live:
                assert live.get("status", "active") == "active", (
                    f"dry_run should not deprecate {src_mem['id'][:8]}"
                )


class TestMinClusterSizeRespected:
    """min_cluster_size=3 but only a 2-memory cluster → no merge."""

    def test_min_cluster_size_respected(self, tmp_vault, tmp_cortex_dir):
        from memem import consolidation, obsidian_store
        importlib.reload(obsidian_store)
        importlib.reload(consolidation)

        near_vec = _fake_embedding("typescript")
        far_vec = _fake_embedding_low("other topic")

        m1 = _save_test_memory("TypeScript over JavaScript.", "TS pref A")
        m2 = _save_test_memory("Use TypeScript for new code.", "TS pref B")
        # Third memory is on a very different topic (low cosine with m1, m2)
        m3 = _save_test_memory("Always use Docker for deployment.", "Docker deployment")

        def _mock_compute(memories: list[dict]):
            vecs = []
            for m in memories:
                if "Docker" in m.get("title", ""):
                    vecs.append(far_vec)
                else:
                    vecs.append(near_vec)
            return list(memories), vecs

        fake_run = MagicMock()

        with patch.object(consolidation, "_compute_embeddings", _mock_compute):
            with patch("memem.consolidation.subprocess.run", fake_run):
                result = consolidation.run_consolidation_pass(
                    layer=2,
                    min_cluster_size=3,  # requires 3, but m1+m2 cluster is only size 2
                    similarity_threshold=0.85,
                    dry_run=False,
                )

        assert result.clusters_processed == 0
        assert result.canonical_memories_created == []
        fake_run.assert_not_called()
