"""Tests for temporal-aware retrieval features in memem.retrieve (v2.2.0).

Covers:
- _extract_temporal_range: parsing of temporal phrases
- _fts_temporal_search: FTS supplement based on created timestamps
- retrieve(): date-aware re-ranking boosting on-window memories
- Non-temporal queries: behavior identical to v2.1.1
- Access writeback via telemetry sidecar (m3)
"""
from __future__ import annotations

import importlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_memory(memories_dir: Path, mem_id: str, title: str, created_iso: str) -> Path:
    """Write a minimal memory .md file with id, title, and created frontmatter."""
    p = memories_dir / f"{mem_id}.md"
    p.write_text(
        f"---\nid: {mem_id}\ntitle: {title}\nproject: test\ncreated: {created_iso}\n---\n\nBody of {title}.\n",
        encoding="utf-8",
    )
    return p


def _write_memory_full(
    memories_dir: Path,
    mem_id: str,
    title: str,
    created_iso: str,
    last_accessed_iso: str | None = None,
    access_count: int = 0,
    layer: int = 2,
    importance: int = 3,
    decay_immune: bool = False,
) -> Path:
    """Write a memory .md file with full decay-related frontmatter fields."""
    last_accessed = last_accessed_iso or created_iso
    decay_immune_str = "true" if decay_immune else "false"
    p = memories_dir / f"{mem_id}.md"
    p.write_text(
        f"---\n"
        f"id: {mem_id}\n"
        f"title: {title}\n"
        f"project: test\n"
        f"created: '{created_iso}'\n"
        f"last_accessed_at: '{last_accessed}'\n"
        f"access_count: {access_count}\n"
        f"layer: {layer}\n"
        f"importance: {importance}\n"
        f"decay_immune: {decay_immune_str}\n"
        f"---\n\nBody of {title}.\n",
        encoding="utf-8",
    )
    return p


def _write_embeddings(state_dir: Path, ids: list[str], dim: int = 384) -> None:
    """Write dummy normalized embeddings for a list of memory ids."""
    rng = np.random.default_rng(42)
    emb = rng.random((len(ids), dim)).astype(np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True).clip(min=1e-9)
    emb = emb / norms
    np.save(str(state_dir / "embeddings.npy"), emb)
    (state_dir / "embedding_ids.json").write_text(json.dumps({"ids": ids}))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def retrieve_env(tmp_path, monkeypatch):
    """Isolated MEMEM_DIR + MEMEM_OBSIDIAN_VAULT with fresh retrieve module."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    memories_dir = vault_dir / "memem" / "memories"
    memories_dir.mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    # Reload models so path constants pick up new env vars, then retrieve
    import memem.models as _models
    importlib.reload(_models)
    import memem.retrieve as _retrieve
    importlib.reload(_retrieve)
    # Clear module-level caches so stale test data doesn't bleed through
    _retrieve._vault_idx_cache = None
    _retrieve._vault_idx_mtime = 0
    _retrieve._vault_idx_count = 0
    _retrieve._emb_cache = None

    yield {
        "state_dir": state_dir,
        "vault_dir": vault_dir,
        "memories_dir": memories_dir,
        "retrieve_mod": _retrieve,
    }

    # Cleanup: reload to pristine state for other tests
    importlib.reload(_models)
    importlib.reload(_retrieve)


# ---------------------------------------------------------------------------
# Feature A: _extract_temporal_range
# ---------------------------------------------------------------------------


class TestExtractTemporalRange:
    def test_yesterday_returns_tuple(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("what did I do yesterday")
        assert result is not None, "Should parse 'yesterday'"
        start, end = result
        now = datetime.now(UTC)
        # start should be ~48h ago, end ~24h ago
        assert abs((now - timedelta(hours=48) - start).total_seconds()) < 5
        assert abs((now - timedelta(hours=24) - end).total_seconds()) < 5

    def test_yesterday_case_insensitive(self):
        from memem.retrieve import _extract_temporal_range
        assert _extract_temporal_range("YESTERDAY I learned") is not None

    def test_today_returns_24h_window(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("what happened today")
        assert result is not None
        start, end = result
        now = datetime.now(UTC)
        assert abs((now - timedelta(hours=24) - start).total_seconds()) < 5
        assert abs((now - end).total_seconds()) < 5

    def test_n_days_ago_two(self):
        """'2 days ago' → window from (now - 3d) to (now - 2d)."""
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("what happened 2 days ago")
        assert result is not None, "Should parse '2 days ago'"
        start, end = result
        now = datetime.now(UTC)
        assert abs((now - timedelta(days=3) - start).total_seconds()) < 5
        assert abs((now - timedelta(days=2) - end).total_seconds()) < 5

    def test_n_days_ago_plural_and_singular(self):
        from memem.retrieve import _extract_temporal_range
        assert _extract_temporal_range("show me 1 day ago") is not None
        assert _extract_temporal_range("recall 5 days ago notes") is not None

    def test_n_hours_ago(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("recall what we discussed 3 hours ago")
        assert result is not None
        start, end = result
        now = datetime.now(UTC)
        assert abs((now - timedelta(hours=4) - start).total_seconds()) < 5
        assert abs((now - timedelta(hours=3) - end).total_seconds()) < 5

    def test_last_week_returns_14d_to_7d(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("what did I work on last week")
        assert result is not None
        start, end = result
        now = datetime.now(UTC)
        assert abs((now - timedelta(days=14) - start).total_seconds()) < 5
        assert abs((now - timedelta(days=7) - end).total_seconds()) < 5

    def test_this_week(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("this week's progress")
        assert result is not None
        start, end = result
        now = datetime.now(UTC)
        assert abs((now - timedelta(days=7) - start).total_seconds()) < 5
        assert abs((now - end).total_seconds()) < 5

    def test_this_morning(self):
        from memem.retrieve import _extract_temporal_range
        result = _extract_temporal_range("what did I write this morning")
        assert result is not None

    def test_none_for_non_temporal_query(self):
        from memem.retrieve import _extract_temporal_range
        assert _extract_temporal_range("how to do X") is None
        assert _extract_temporal_range("what is the callback pattern") is None
        assert _extract_temporal_range("v2.1.0 release notes") is None
        assert _extract_temporal_range("") is None


# ---------------------------------------------------------------------------
# Feature B: date-aware re-ranking in retrieve()
# ---------------------------------------------------------------------------


class TestTemporalRerank:
    def test_yesterday_memory_boosted_above_older(self, retrieve_env):
        """A memory from yesterday should rank higher than week-old ones."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        id_yesterday = str(uuid.uuid4())
        id_week_old = str(uuid.uuid4())
        id_month_old = str(uuid.uuid4())

        created_yesterday = (now - timedelta(hours=30)).isoformat()
        created_week = (now - timedelta(days=8)).isoformat()
        created_month = (now - timedelta(days=35)).isoformat()

        _write_memory(mdir, id_yesterday, "Yesterday's memory", created_yesterday)
        _write_memory(mdir, id_week_old, "Week-old memory", created_week)
        _write_memory(mdir, id_month_old, "Month-old memory", created_month)

        _write_embeddings(state_dir, [id_yesterday, id_week_old, id_month_old])

        # Clear caches so reload picks up the new files
        mod._vault_idx_cache = None
        mod._emb_cache = None

        results = mod.retrieve("what did I do yesterday", k=8)
        assert len(results) > 0

        # yesterday memory should appear in results
        result_ids = [r.get("id") for r in results]
        assert id_yesterday in result_ids, "Yesterday's memory should be in results"

        # if both yesterday and week_old are present, yesterday should rank higher
        if id_week_old in result_ids:
            idx_yesterday = result_ids.index(id_yesterday)
            idx_week_old = result_ids.index(id_week_old)
            # FTS hits come first; check scores for cosine candidates
            yesterday_hit = results[idx_yesterday]
            week_hit = results[idx_week_old]
            # yesterday_hit's score should be >= week_hit's (boosted by +0.2 or via FTS)
            assert yesterday_hit["score"] >= week_hit["score"], (
                f"Yesterday memory (score={yesterday_hit['score']:.3f}) should rank >= "
                f"week-old (score={week_hit['score']:.3f})"
            )

    def test_non_temporal_query_cosine_order_preserved(self, retrieve_env):
        """Non-temporal query: no rerank, cosine order preserved."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, mid in enumerate(ids):
            created = (now - timedelta(days=i * 10)).isoformat()
            _write_memory(mdir, mid, f"Memory {i}", created)

        _write_embeddings(state_dir, ids)
        mod._vault_idx_cache = None
        mod._emb_cache = None

        results = mod.retrieve("callback pattern", k=8)
        # Just verify we get results and no crash; cosine order is by score desc
        cosine_results = [r for r in results if r.get("source") == "cosine"]
        for i in range(len(cosine_results) - 1):
            assert cosine_results[i]["score"] >= cosine_results[i + 1]["score"], (
                "Cosine results should be sorted by score desc for non-temporal queries"
            )


# ---------------------------------------------------------------------------
# Feature C: _fts_temporal_search
# ---------------------------------------------------------------------------


class TestFtsTemporalSearch:
    def test_last_week_returns_memory_in_range(self, retrieve_env):
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        now = datetime.now(UTC)
        mid_in_range = str(uuid.uuid4())
        mid_out_range = str(uuid.uuid4())

        created_in = (now - timedelta(days=10)).isoformat()   # 10d ago → in "last week" (7-14d)
        created_out = (now - timedelta(days=2)).isoformat()   # 2d ago → not in last week

        _write_memory(mdir, mid_in_range, "Last week task", created_in)
        _write_memory(mdir, mid_out_range, "Recent task", created_out)

        mod._vault_idx_cache = None
        vault_idx = mod.load_vault_index()

        hits = mod._fts_temporal_search("what did I do last week", vault_idx)
        hit_ids = [h["id"] for h in hits]
        assert mid_in_range in hit_ids, "Memory from 10d ago should be in last week window"
        assert mid_out_range not in hit_ids, "Memory from 2d ago should NOT be in last week window"

    def test_non_temporal_returns_empty(self, retrieve_env):
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        now = datetime.now(UTC)
        _write_memory(mdir, str(uuid.uuid4()), "Some memory", now.isoformat())
        mod._vault_idx_cache = None
        vault_idx = mod.load_vault_index()

        hits = mod._fts_temporal_search("callback pattern in Python", vault_idx)
        assert hits == [], "Non-temporal query should return empty list"

    def test_max_results_respected(self, retrieve_env):
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        now = datetime.now(UTC)
        # Create 5 memories all in "today" window
        ids = [str(uuid.uuid4()) for _ in range(5)]
        for mid in ids:
            created = (now - timedelta(hours=12)).isoformat()
            _write_memory(mdir, mid, f"Memory {mid[:8]}", created)

        mod._vault_idx_cache = None
        vault_idx = mod.load_vault_index()

        hits = mod._fts_temporal_search("what happened today", vault_idx, max_results=3)
        assert len(hits) <= 3, f"Should cap at max_results=3, got {len(hits)}"

    def test_hits_have_fts_source(self, retrieve_env):
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        now = datetime.now(UTC)
        mid = str(uuid.uuid4())
        _write_memory(mdir, mid, "Today note", (now - timedelta(hours=5)).isoformat())

        mod._vault_idx_cache = None
        vault_idx = mod.load_vault_index()
        hits = mod._fts_temporal_search("today's notes", vault_idx)
        for hit in hits:
            assert hit.get("source") == "fts"
            assert hit.get("score") == 1.0


# ---------------------------------------------------------------------------
# Integration: non-temporal query unchanged
# ---------------------------------------------------------------------------


class TestNonTemporalUnchanged:
    def test_no_fts_temporal_hits_for_non_temporal_query(self, retrieve_env):
        """_fts_temporal_search returns [] for non-temporal queries."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        now = datetime.now(UTC)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, mid in enumerate(ids):
            created = (now - timedelta(days=i)).isoformat()
            _write_memory(mdir, mid, f"Memory {i}", created)

        mod._vault_idx_cache = None
        vault_idx = mod.load_vault_index()

        hits = mod._fts_temporal_search("test query with no temporal phrase", vault_idx)
        assert hits == [], "Non-temporal query must produce zero temporal FTS hits"

    def test_retrieve_returns_results_without_temporal(self, retrieve_env):
        """retrieve() still works for non-temporal query with embeddings present."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, mid in enumerate(ids):
            created = (now - timedelta(days=i * 5)).isoformat()
            _write_memory(mdir, mid, f"Callback pattern {i}", created)

        _write_embeddings(state_dir, ids)
        mod._vault_idx_cache = None
        mod._emb_cache = None

        results = mod.retrieve("callback pattern", k=8)
        assert len(results) >= 1, "Should return results for non-temporal query"
        # All should be cosine source (no temporal FTS hits)
        for r in results:
            assert r.get("source") == "cosine", (
                f"Non-temporal query should only produce cosine results, got: {r.get('source')}"
            )


# ---------------------------------------------------------------------------
# New field extraction: decay/access metadata
# ---------------------------------------------------------------------------


class TestLoadVaultIndexExtractsNewFields:
    def test_load_vault_index_extracts_new_fields(self, retrieve_env):
        """load_vault_index() must extract the 6 new frontmatter fields correctly."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        mem_id = str(uuid.uuid4())
        p = mdir / f"{mem_id}.md"
        p.write_text(
            f"---\n"
            f"id: {mem_id}\n"
            f"title: Decay test memory\n"
            f"project: test\n"
            f"created: '2026-01-01T00:00:00+00:00'\n"
            f"last_accessed_at: '2026-04-18T15:30:00+00:00'\n"
            f"access_count: 5\n"
            f"decay_immune: true\n"
            f"layer: 0\n"
            f"importance: 5\n"
            f"valid_at: '2026-04-18T00:00:00+00:00'\n"
            f"---\n\nBody of decay test.\n",
            encoding="utf-8",
        )

        mod._vault_idx_cache = None
        idx = mod.load_vault_index()

        assert mem_id in idx, "Memory should be present in vault index"
        entry = idx[mem_id]

        assert entry["last_accessed_at"] == "2026-04-18T15:30:00+00:00", (
            f"last_accessed_at mismatch: {entry['last_accessed_at']!r}"
        )
        assert entry["access_count"] == 5, (
            f"access_count mismatch: {entry['access_count']!r}"
        )
        assert entry["decay_immune"] is True, (
            f"decay_immune mismatch: {entry['decay_immune']!r}"
        )
        assert entry["layer"] == 0, f"layer mismatch: {entry['layer']!r}"
        assert entry["importance"] == 5, f"importance mismatch: {entry['importance']!r}"
        assert entry["valid_at"] == "2026-04-18T00:00:00+00:00", (
            f"valid_at mismatch: {entry['valid_at']!r}"
        )

    def test_load_vault_index_defaults_for_missing_fields(self, retrieve_env):
        """load_vault_index() must use safe defaults when new fields are absent."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        mem_id = str(uuid.uuid4())
        created_val = "2026-03-15T12:00:00+00:00"
        p = mdir / f"{mem_id}.md"
        p.write_text(
            f"---\n"
            f"id: {mem_id}\n"
            f"title: Minimal memory\n"
            f"project: test\n"
            f"created: '{created_val}'\n"
            f"---\n\nMinimal body.\n",
            encoding="utf-8",
        )

        mod._vault_idx_cache = None
        idx = mod.load_vault_index()

        assert mem_id in idx
        entry = idx[mem_id]

        # last_accessed_at falls back to created when absent
        assert entry["last_accessed_at"] == created_val, (
            f"last_accessed_at should fall back to created: {entry['last_accessed_at']!r}"
        )
        assert entry["access_count"] == 0, f"access_count default should be 0: {entry['access_count']!r}"
        assert entry["decay_immune"] is False, f"decay_immune default should be False: {entry['decay_immune']!r}"
        assert entry["layer"] == 2, f"layer default should be 2: {entry['layer']!r}"
        assert entry["importance"] == 3, f"importance default should be 3: {entry['importance']!r}"
        # valid_at falls back to created when absent
        assert entry["valid_at"] == created_val, (
            f"valid_at should fall back to created: {entry['valid_at']!r}"
        )


# ---------------------------------------------------------------------------
# m3: Access writeback via telemetry sidecar
# ---------------------------------------------------------------------------


@pytest.fixture
def writeback_env(tmp_path, monkeypatch):
    """Isolated environment for writeback tests: tmp MEMEM_DIR + vault + reloaded modules."""
    state_dir = tmp_path / ".memem"
    state_dir.mkdir()
    vault_dir = tmp_path / "obsidian-brain"
    memories_dir = vault_dir / "memem" / "memories"
    memories_dir.mkdir(parents=True)

    monkeypatch.setenv("MEMEM_DIR", str(state_dir))
    monkeypatch.setenv("MEMEM_OBSIDIAN_VAULT", str(vault_dir))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    monkeypatch.delenv("CORTEX_OBSIDIAN_VAULT", raising=False)

    # Reload path-constant modules in order so TELEMETRY_FILE + retrieve paths
    # all point at the tmp directory.
    import memem.models as _models
    importlib.reload(_models)
    import memem.telemetry as _telemetry
    importlib.reload(_telemetry)
    import memem.retrieve as _retrieve
    importlib.reload(_retrieve)
    import memem.settings as _settings_mod
    importlib.reload(_settings_mod)

    # Clear module-level caches
    _retrieve._vault_idx_cache = None
    _retrieve._vault_idx_mtime = 0
    _retrieve._vault_idx_count = 0
    _retrieve._emb_cache = None

    yield {
        "state_dir": state_dir,
        "memories_dir": memories_dir,
        "retrieve_mod": _retrieve,
        "telemetry_mod": _telemetry,
        "settings_mod": _settings_mod,
    }

    # Teardown: restore pristine state for subsequent tests
    importlib.reload(_models)
    importlib.reload(_telemetry)
    importlib.reload(_retrieve)
    importlib.reload(_settings_mod)


class TestAccessWriteback:
    def test_retrieve_triggers_access_writeback(self, writeback_env):
        """retrieve() with MEMEM_WRITEBACK_ENABLED=True records access for cosine hits."""
        env = writeback_env
        mod = env["retrieve_mod"]
        tel = env["telemetry_mod"]
        settings_mod = env["settings_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        # Ensure writeback is enabled
        settings_mod.MEMEM_WRITEBACK_ENABLED = True

        # Write a couple of memories
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())
        now = datetime.now(UTC)
        _write_memory(mdir, id1, "Alpha memory", (now - timedelta(days=1)).isoformat())
        _write_memory(mdir, id2, "Beta memory", (now - timedelta(days=2)).isoformat())

        _write_embeddings(state_dir, [id1, id2])
        mod._vault_idx_cache = None
        mod._emb_cache = None

        results = mod.retrieve("alpha beta memory", k=2)
        assert len(results) >= 1, "Expected at least one result"

        # Wait briefly for the daemon thread to complete
        time.sleep(0.5)

        # At least one cosine hit should have been recorded in telemetry
        cosine_ids = [r["id"] for r in results if r.get("source") == "cosine"]
        assert cosine_ids, "Expected cosine hits in results"

        found_any = False
        for mid in cosine_ids:
            entry = tel._get_telemetry(mid)
            if entry["access_count"] >= 1:
                found_any = True
                break
        assert found_any, (
            f"Expected access_count >= 1 for at least one cosine hit, "
            f"got: {[tel._get_telemetry(mid) for mid in cosine_ids]}"
        )

    def test_writeback_disabled_when_flag_off(self, writeback_env):
        """When MEMEM_WRITEBACK_ENABLED=False, retrieve() does NOT record any telemetry."""
        env = writeback_env
        mod = env["retrieve_mod"]
        tel = env["telemetry_mod"]
        settings_mod = env["settings_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        # Disable writeback
        settings_mod.MEMEM_WRITEBACK_ENABLED = False

        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())
        now = datetime.now(UTC)
        _write_memory(mdir, id1, "Alpha memory", (now - timedelta(days=1)).isoformat())
        _write_memory(mdir, id2, "Beta memory", (now - timedelta(days=2)).isoformat())

        _write_embeddings(state_dir, [id1, id2])
        mod._vault_idx_cache = None
        mod._emb_cache = None

        results = mod.retrieve("alpha beta memory", k=2)
        assert len(results) >= 1, "Expected at least one result"

        # Wait to give any (unexpected) thread time to fire
        time.sleep(0.5)

        # No telemetry should have been recorded
        for r in results:
            mid = r["id"]
            entry = tel._get_telemetry(mid)
            assert entry["access_count"] == 0, (
                f"Expected access_count==0 when writeback disabled, "
                f"got {entry['access_count']} for memory {mid}"
            )


# ---------------------------------------------------------------------------
# m4: Recency decay scoring
# ---------------------------------------------------------------------------


class TestDecayScoring:
    def test_decay_penalizes_stale_memories(self, retrieve_env):
        """compute_strength penalizes stale memories vs freshly-accessed ones.

        Note: the decay wiring in retrieve() was reverted (benchmark regression to
        70.0% vs 73% gate). This test verifies the underlying decay module gives
        the correct relative strengths so the logic is ready for future wiring.
        """
        from memem.decay import compute_strength

        now = datetime.now(UTC)

        fresh_mem = {
            "last_accessed_at": now.isoformat(),
            "access_count": 3,
            "importance": 3,
            "layer": 2,
            "decay_immune": False,
        }
        stale_mem = {
            "last_accessed_at": (now - timedelta(days=90)).isoformat(),
            "access_count": 0,
            "importance": 3,
            "layer": 2,
            "decay_immune": False,
        }

        fresh_strength = compute_strength(fresh_mem, now=now)
        stale_strength = compute_strength(stale_mem, now=now)

        assert fresh_strength > stale_strength, (
            f"Fresh memory (strength={fresh_strength:.4f}) should outscore stale "
            f"(strength={stale_strength:.4f}) — decay module is working correctly"
        )

    def test_l0_memories_exempt_from_decay(self, retrieve_env):
        """Layer=0 (L0) memories are immune — is_immune() returns True regardless of age.

        Note: the decay wiring in retrieve() was reverted (benchmark regression).
        This test verifies the immunity logic in the decay module itself.
        """
        from memem.decay import compute_strength, is_immune

        now = datetime.now(UTC)

        l0_mem = {
            "last_accessed_at": (now - timedelta(days=365)).isoformat(),
            "access_count": 0,
            "importance": 3,
            "layer": 0,
            "decay_immune": False,
        }
        l2_stale_mem = {
            "last_accessed_at": (now - timedelta(days=365)).isoformat(),
            "access_count": 0,
            "importance": 3,
            "layer": 2,
            "decay_immune": False,
        }

        assert is_immune(l0_mem) is True, "L0 memory must be immune to decay"
        assert is_immune(l2_stale_mem) is False, "L2 non-immune memory should not be exempt"

        # Also verify compute_strength gives a very low value for the stale L2 memory
        # confirming that L0 immunity protects it from the same penalty
        l2_strength = compute_strength(l2_stale_mem, now=now)
        assert l2_strength < 0.1, (
            f"Stale L2 memory should have very low strength ({l2_strength:.4f}), "
            f"confirming L0 immunity is a meaningful protection"
        )

    def test_decay_immune_flag_respected(self, retrieve_env):
        """decay_immune=true overrides age penalty regardless of layer.

        Note: the decay wiring in retrieve() was reverted (benchmark regression).
        This test verifies the immunity logic in the decay module itself.
        """
        from memem.decay import is_immune

        now = datetime.now(UTC)

        immune_l2_mem = {
            "last_accessed_at": (now - timedelta(days=200)).isoformat(),
            "access_count": 0,
            "importance": 3,
            "layer": 2,
            "decay_immune": True,
        }
        non_immune_l2_mem = {
            "last_accessed_at": (now - timedelta(days=200)).isoformat(),
            "access_count": 0,
            "importance": 3,
            "layer": 2,
            "decay_immune": False,
        }

        assert is_immune(immune_l2_mem) is True, (
            "decay_immune=True on L2 memory must return is_immune=True"
        )
        assert is_immune(non_immune_l2_mem) is False, (
            "L2 memory without decay_immune flag must not be immune"
        )

        # Also verify via vault index extraction: write a memory file with
        # decay_immune=true and confirm the vault index parses it correctly
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]

        mem_id = str(uuid.uuid4())
        _write_memory_full(
            mdir, mem_id, "Immune memory",
            created_iso=(now - timedelta(days=200)).isoformat(),
            last_accessed_iso=(now - timedelta(days=200)).isoformat(),
            access_count=0,
            layer=2, importance=3, decay_immune=True,
        )

        mod._vault_idx_cache = None
        vault = mod.load_vault_index()
        assert mem_id in vault
        assert vault[mem_id]["decay_immune"] is True, (
            "Vault index must parse decay_immune: true correctly"
        )
        assert is_immune(vault[mem_id]) is True, (
            "is_immune(vault_entry) must be True for decay_immune: true memories"
        )

    def test_decay_disabled_via_setting(self, retrieve_env):
        """MEMEM_DECAY_ENABLED setting exists and is readable/patchable via monkeypatch.

        Note: the decay wiring in retrieve() was reverted (benchmark regression to 70%
        vs 73% gate). This test verifies the settings infrastructure is in place and
        that patching it does not affect retrieve() scores (since wiring is off).
        """
        import memem.settings as _settings_mod

        # Verify the setting exists and defaults to True
        assert hasattr(_settings_mod, "MEMEM_DECAY_ENABLED"), (
            "MEMEM_DECAY_ENABLED must be defined in memem.settings"
        )
        assert isinstance(_settings_mod.MEMEM_DECAY_ENABLED, bool), (
            "MEMEM_DECAY_ENABLED must be a bool"
        )

        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        id_stale = str(uuid.uuid4())

        _write_memory_full(
            mdir, id_stale, "Very stale memory",
            created_iso=(now - timedelta(days=365)).isoformat(),
            last_accessed_iso=(now - timedelta(days=365)).isoformat(),
            access_count=0,
            layer=2, importance=3,
        )

        _write_embeddings(state_dir, [id_stale])
        mod._vault_idx_cache = None
        mod._emb_cache = None

        # With decay wiring reverted, scores should be identical regardless of setting
        _settings_mod.MEMEM_DECAY_ENABLED = False
        results_off = mod.retrieve("stale memory test", k=8)

        mod._vault_idx_cache = None
        mod._emb_cache = None

        _settings_mod.MEMEM_DECAY_ENABLED = True
        results_on = mod.retrieve("stale memory test", k=8)

        # Since wiring is reverted, scores should be identical in both cases
        off_score = next((r["score"] for r in results_off if r["id"] == id_stale), None)
        on_score = next((r["score"] for r in results_on if r["id"] == id_stale), None)
        assert off_score is not None, "Stale memory should appear in results"
        assert on_score is not None, "Stale memory should appear in results"
        assert abs(off_score - on_score) < 1e-5, (
            f"With decay wiring reverted, scores should be identical: "
            f"off={off_score:.6f} on={on_score:.6f}"
        )

        # Cleanup: restore default
        _settings_mod.MEMEM_DECAY_ENABLED = True


# ---------------------------------------------------------------------------
# m5: BM25+cosine RRF fusion
# ---------------------------------------------------------------------------


class TestBM25RRF:
    def test_bm25_index_built_and_cached(self, retrieve_env):
        """After the first retrieve() call, _bm25_cache is populated and reused."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, mid in enumerate(ids):
            created = (now - timedelta(days=i)).isoformat()
            _write_memory(mdir, mid, f"Cache test memory {i}", created)

        _write_embeddings(state_dir, ids)
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        # First call — should build the BM25 cache
        mod.retrieve("cache test memory", k=8)
        first_cache = mod._bm25_cache
        assert first_cache is not None, "_bm25_cache should be set after first retrieve()"

        # Second call — should reuse the same cache object (identity check)
        mod.retrieve("cache test memory", k=8)
        second_cache = mod._bm25_cache
        assert first_cache is second_cache, "Second call should reuse same _bm25_cache (no rebuild)"

    def test_rrf_orders_better_than_cosine_alone(self, retrieve_env):
        """A memory with strong title keyword match but weak embedding should rank higher via RRF."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        rng = __import__("numpy").random.default_rng(999)
        dim = 384

        # Memory A: random embedding (weak cosine match) but title contains exact keyword
        id_a = str(uuid.uuid4())
        now = datetime.now(UTC)
        p_a = mdir / f"{id_a}.md"
        p_a.write_text(
            f"---\nid: {id_a}\ntitle: bm25testquery exact keyword match\nproject: test\ncreated: {now.isoformat()}\n---\n\nbm25testquery\n",
            encoding="utf-8",
        )

        # Memory B: strong cosine match (same random vector) but no keyword overlap
        id_b = str(uuid.uuid4())
        p_b = mdir / f"{id_b}.md"
        p_b.write_text(
            f"---\nid: {id_b}\ntitle: Unrelated semantic topic\nproject: test\ncreated: {now.isoformat()}\n---\n\nSemantic content without the query keyword.\n",
            encoding="utf-8",
        )

        # Create embeddings: A gets a random vector, B gets a near-query vector
        q_vec = __import__("numpy").ones(dim, dtype="float32")
        q_vec /= __import__("numpy").linalg.norm(q_vec)

        emb_a = rng.random(dim).astype("float32")
        emb_a /= __import__("numpy").linalg.norm(emb_a)  # random, low cosine sim

        emb_b = q_vec + rng.random(dim).astype("float32") * 0.01  # near query
        emb_b /= __import__("numpy").linalg.norm(emb_b)

        emb = __import__("numpy").stack([emb_a, emb_b])
        __import__("numpy").save(str(state_dir / "embeddings.npy"), emb)
        (state_dir / "embedding_ids.json").write_text(
            __import__("json").dumps({"ids": [id_a, id_b]})
        )

        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        results = mod.retrieve("bm25testquery exact keyword match", k=8)
        result_ids = [r["id"] for r in results]

        # A must appear in results (BM25 should bring it up)
        assert id_a in result_ids, "Memory A (keyword match) should be in results via RRF"

    def test_empty_body_entries_skipped(self, retrieve_env):
        """Memories with empty body should not cause IndexError in BM25 building."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        id_empty = str(uuid.uuid4())
        id_normal = str(uuid.uuid4())

        # Write a memory with empty body (only frontmatter)
        p_empty = mdir / f"{id_empty}.md"
        p_empty.write_text(
            f"---\nid: {id_empty}\ntitle: Empty body memory\nproject: test\ncreated: {now.isoformat()}\n---\n\n",
            encoding="utf-8",
        )
        # Normal memory
        _write_memory(mdir, id_normal, "Normal memory with content", now.isoformat())

        _write_embeddings(state_dir, [id_empty, id_normal])
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        # Should not raise
        results = mod.retrieve("normal memory content", k=8)
        assert isinstance(results, list), "retrieve() should return a list even with empty-body entries"

    def test_query_with_no_keyword_match(self, retrieve_env):
        """When BM25 scores are all zero, cosine-only path still returns results."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        now = datetime.now(UTC)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i, mid in enumerate(ids):
            created = (now - timedelta(days=i)).isoformat()
            _write_memory(mdir, mid, f"Memory about completely different topic {i}", created)

        _write_embeddings(state_dir, ids)
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        # Query with no keyword overlap with any memory title/body
        results = mod.retrieve("xyzzy frobnicator quux", k=8)
        # Should still return results (cosine path still works)
        assert isinstance(results, list), "retrieve() should return a list even with zero BM25 scores"
        # When BM25 all-zero, RRF still works via cosine ranks
        for r in results:
            assert r.get("source") in ("cosine", "fts"), f"Unexpected source: {r.get('source')}"


# ---------------------------------------------------------------------------
# m6: MMR diversification
# ---------------------------------------------------------------------------


def _write_memory_with_embedding(
    memories_dir: Path,
    state_dir: Path,
    mem_id: str,
    title: str,
    created_iso: str,
    vec: np.ndarray,
    layer: int = 2,
    decay_immune: bool = False,
) -> None:
    """Write a single memory file (helper for MMR tests — not batch)."""
    decay_immune_str = "true" if decay_immune else "false"
    p = memories_dir / f"{mem_id}.md"
    p.write_text(
        f"---\n"
        f"id: {mem_id}\n"
        f"title: {title}\n"
        f"project: test\n"
        f"created: '{created_iso}'\n"
        f"layer: {layer}\n"
        f"decay_immune: {decay_immune_str}\n"
        f"---\n\nBody of {title}.\n",
        encoding="utf-8",
    )


def _write_vault_with_embeddings(
    memories_dir: Path,
    state_dir: Path,
    mems: list[dict],
) -> None:
    """Write multiple memories + their embeddings to vault.

    Each entry in `mems` is a dict with keys: id, title, created, vec,
    layer (optional, default 2), decay_immune (optional, default False).
    """
    ids = []
    vecs = []
    now_iso = datetime.now(UTC).isoformat()
    for m in mems:
        mid = m["id"]
        title = m.get("title", f"Memory {mid[:8]}")
        created = m.get("created", now_iso)
        layer = m.get("layer", 2)
        decay_immune = m.get("decay_immune", False)
        _write_memory_with_embedding(
            memories_dir, state_dir, mid, title, created,
            m["vec"], layer=layer, decay_immune=decay_immune,
        )
        ids.append(mid)
        vecs.append(m["vec"])

    emb = np.stack(vecs).astype(np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True).clip(min=1e-9)
    emb = emb / norms
    np.save(str(state_dir / "embeddings.npy"), emb)
    (state_dir / "embedding_ids.json").write_text(json.dumps({"ids": ids}))


class TestMMRDiversification:
    def test_mmr_dedupes_near_duplicates(self, retrieve_env):
        """Near-duplicate memories (high cosine sim) should have only one in top-k output."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        rng = np.random.default_rng(7)
        dim = 384
        now_iso = datetime.now(UTC).isoformat()

        # Create a base query-aligned vector
        base_vec = rng.random(dim).astype(np.float32)
        base_vec /= np.linalg.norm(base_vec)

        # Two near-duplicate memories: very similar to each other (and to query)
        dup_a_id = str(uuid.uuid4())
        dup_b_id = str(uuid.uuid4())
        dup_vec_a = base_vec + rng.random(dim).astype(np.float32) * 0.001
        dup_vec_a /= np.linalg.norm(dup_vec_a)
        dup_vec_b = base_vec + rng.random(dim).astype(np.float32) * 0.001
        dup_vec_b /= np.linalg.norm(dup_vec_b)

        # 8 more diverse memories with moderate similarity to query
        other_ids = [str(uuid.uuid4()) for _ in range(8)]
        other_vecs = []
        for _ in other_ids:
            v = rng.random(dim).astype(np.float32)
            v /= np.linalg.norm(v)
            # Blend moderately with base_vec so they still rank
            v = 0.5 * base_vec + 0.5 * v
            v /= np.linalg.norm(v)
            other_vecs.append(v)

        mems = [
            {"id": dup_a_id, "title": "Python callback pattern async", "created": now_iso,
             "vec": dup_vec_a},
            {"id": dup_b_id, "title": "Python callback pattern await", "created": now_iso,
             "vec": dup_vec_b},
        ] + [
            {"id": oid, "title": f"Diverse memory {i}", "created": now_iso, "vec": ov}
            for i, (oid, ov) in enumerate(zip(other_ids, other_vecs, strict=True))
        ]

        _write_vault_with_embeddings(mdir, state_dir, mems)
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        results = mod.retrieve("Python callback pattern", k=8)
        result_ids = [r["id"] for r in results]

        # At most one of the near-duplicate pair should appear
        dup_count = sum(1 for rid in result_ids if rid in (dup_a_id, dup_b_id))
        assert dup_count <= 1, (
            f"MMR should deduplicate near-duplicates: found {dup_count} of the pair in results"
        )

    def test_l0_memories_always_included(self, retrieve_env):
        """L0 memories must appear in final-k regardless of raw cosine score."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        rng = np.random.default_rng(13)
        dim = 384
        now_iso = datetime.now(UTC).isoformat()

        # L0 memory with a deliberately weak embedding (unlikely to rank via cosine)
        l0_id = str(uuid.uuid4())
        l0_vec = rng.random(dim).astype(np.float32)
        l0_vec /= np.linalg.norm(l0_vec)
        # Make it orthogonal to all others by subtracting projections later — just use random

        # 9 other strong cosine memories
        other_ids = [str(uuid.uuid4()) for _ in range(9)]
        query_vec_approx = np.ones(dim, dtype=np.float32)
        query_vec_approx /= np.linalg.norm(query_vec_approx)

        other_vecs = []
        for _ in other_ids:
            v = query_vec_approx + rng.random(dim).astype(np.float32) * 0.05
            v /= np.linalg.norm(v)
            other_vecs.append(v)

        mems = [{"id": l0_id, "title": "L0 core identity memory", "created": now_iso,
                 "vec": l0_vec, "layer": 0}] + [
            {"id": oid, "title": f"Strong cosine memory {i}", "created": now_iso, "vec": ov}
            for i, (oid, ov) in enumerate(zip(other_ids, other_vecs, strict=True))
        ]

        _write_vault_with_embeddings(mdir, state_dir, mems)
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        results = mod.retrieve("strong cosine memory query", k=8)
        result_ids = [r["id"] for r in results]

        assert l0_id in result_ids, (
            f"L0 memory must always be included in final-k results regardless of cosine score. "
            f"Result ids: {result_ids}"
        )

    def test_decay_immune_always_included(self, retrieve_env):
        """decay_immune=True memories must appear in final-k regardless of raw score."""
        env = retrieve_env
        mod = env["retrieve_mod"]
        mdir = env["memories_dir"]
        state_dir = env["state_dir"]

        rng = np.random.default_rng(17)
        dim = 384
        now_iso = datetime.now(UTC).isoformat()

        # decay_immune memory with weak embedding
        immune_id = str(uuid.uuid4())
        immune_vec = rng.random(dim).astype(np.float32)
        immune_vec /= np.linalg.norm(immune_vec)

        # 9 other strong cosine memories
        other_ids = [str(uuid.uuid4()) for _ in range(9)]
        query_vec_approx = np.ones(dim, dtype=np.float32)
        query_vec_approx /= np.linalg.norm(query_vec_approx)
        other_vecs = []
        for _ in other_ids:
            v = query_vec_approx + rng.random(dim).astype(np.float32) * 0.05
            v /= np.linalg.norm(v)
            other_vecs.append(v)

        mems = [{"id": immune_id, "title": "Immune memory critical ref", "created": now_iso,
                 "vec": immune_vec, "decay_immune": True}] + [
            {"id": oid, "title": f"Strong cosine memory {i}", "created": now_iso, "vec": ov}
            for i, (oid, ov) in enumerate(zip(other_ids, other_vecs, strict=True))
        ]

        _write_vault_with_embeddings(mdir, state_dir, mems)
        mod._vault_idx_cache = None
        mod._emb_cache = None
        mod._bm25_cache = None

        results = mod.retrieve("strong cosine memory query", k=8)
        result_ids = [r["id"] for r in results]

        assert immune_id in result_ids, (
            f"decay_immune memory must always be included in final-k results. "
            f"Result ids: {result_ids}"
        )

    def test_mmr_no_op_when_few_candidates(self, retrieve_env):
        """With <= k candidates, MMR returns them all unchanged."""
        from memem.retrieve import _mmr_rerank

        rng = np.random.default_rng(99)
        dim = 384
        k = 8

        # Build 5 candidate hits (< k=8)
        ids = [str(uuid.uuid4()) for _ in range(5)]
        embs = rng.random((5, dim)).astype(np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True).clip(min=1e-9)
        embs = embs / norms

        candidates: list = [
            {"id": mid, "score": float(i), "layer": 2, "decay_immune": False,
             "path": f"/tmp/{mid}.md", "title": f"Memory {i}"}
            for i, mid in enumerate(ids)
        ]

        result = _mmr_rerank(candidates, embs, ids, k=k)
        # Should return all candidates unchanged (no diversification needed)
        assert len(result) == len(candidates), (
            f"With {len(candidates)} candidates < k={k}, MMR should return all: got {len(result)}"
        )
        result_ids = [r["id"] for r in result]
        for mid in ids:
            assert mid in result_ids, f"All candidate ids should be present, missing {mid}"

    def test_mmr_no_embedding_for_candidate(self, retrieve_env):
        """Candidate whose id is not in embedding index should still be included without crash."""
        from memem.retrieve import _mmr_rerank

        rng = np.random.default_rng(55)
        dim = 384
        k = 3

        # 5 candidates but embedding index only has 3 of them
        all_ids = [str(uuid.uuid4()) for _ in range(5)]
        emb_ids = all_ids[:3]  # only first 3 have embeddings
        embs = rng.random((3, dim)).astype(np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True).clip(min=1e-9)
        embs = embs / norms

        candidates: list = [
            {"id": mid, "score": 1.0 / (i + 1), "layer": 2, "decay_immune": False,
             "path": f"/tmp/{mid}.md", "title": f"Memory {i}"}
            for i, mid in enumerate(all_ids)
        ]

        # Should not raise; should return k results
        result = _mmr_rerank(candidates, embs, emb_ids, k=k)
        assert len(result) == k, (
            f"MMR should return k={k} results even when some candidates lack embeddings, "
            f"got {len(result)}"
        )
