"""Tests for temporal-aware retrieval features in memem.retrieve (v2.2.0).

Covers:
- _extract_temporal_range: parsing of temporal phrases
- _fts_temporal_search: FTS supplement based on created timestamps
- retrieve(): date-aware re-ranking boosting on-window memories
- Non-temporal queries: behavior identical to v2.1.1
"""
from __future__ import annotations

import importlib
import json
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
