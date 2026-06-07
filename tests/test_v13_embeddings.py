"""Tests for m5: MEMEM_USE_EMBEDDINGS env-var gate + startup log line.

Acceptance criteria:
- MEMEM_USE_EMBEDDINGS=0 → embedding NOT called; FTS-only path runs
- MEMEM_USE_EMBEDDINGS=1 (default) → embedding future submitted
- _embeddings_enabled() re-reads the module attribute at each call (runtime-patchable)
- _warmup_model() logs "embedding judge enabled" / "embedding judge disabled" correctly
- MEMEM_USE_EMBEDDINGS is a module-level bool in settings.py
"""
from __future__ import annotations

import os  # noqa: F401 — used in test_memem_use_embeddings_default_is_on

# ---------------------------------------------------------------------------
# Test 1: MEMEM_USE_EMBEDDINGS is a module-level bool constant in settings.py
# ---------------------------------------------------------------------------

def test_memem_use_embeddings_is_module_level_bool():
    """MEMEM_USE_EMBEDDINGS must be a module-level bool in settings.py."""
    import memem.settings as settings

    assert hasattr(settings, "MEMEM_USE_EMBEDDINGS"), "MEMEM_USE_EMBEDDINGS missing from settings"
    assert isinstance(settings.MEMEM_USE_EMBEDDINGS, bool), (
        f"MEMEM_USE_EMBEDDINGS must be bool, got {type(settings.MEMEM_USE_EMBEDDINGS)}"
    )


# ---------------------------------------------------------------------------
# Test 2: _embeddings_enabled() helper exists and reflects the attribute
# ---------------------------------------------------------------------------

def test_embeddings_enabled_helper_exists_and_works(monkeypatch):
    """_embeddings_enabled() must exist in settings and reflect current attribute value."""
    import memem.settings as settings

    assert callable(settings._embeddings_enabled), "_embeddings_enabled must be callable"

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", True)
    assert settings._embeddings_enabled() is True

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", False)
    assert settings._embeddings_enabled() is False


# ---------------------------------------------------------------------------
# Test 3: default value is ON (True) when env var is absent
# ---------------------------------------------------------------------------

def test_memem_use_embeddings_default_is_on():
    """MEMEM_USE_EMBEDDINGS default (env var absent) must be True."""
    import memem.settings as settings

    # We cannot reload without side effects in a shared test session,
    # but we can verify the default raw env var logic
    raw = os.getenv("MEMEM_USE_EMBEDDINGS", "1")
    expected = raw != "0"
    assert expected == settings.MEMEM_USE_EMBEDDINGS


# ---------------------------------------------------------------------------
# Test 4: MEMEM_USE_EMBEDDINGS=0 → embedding NOT submitted in _search_memories_fts
# ---------------------------------------------------------------------------

def test_embeddings_disabled_skips_emb_future(monkeypatch):
    """When MEMEM_USE_EMBEDDINGS=0, _search_embedding_with_scores must NOT be called."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", False)

    emb_called = []

    def _fake_emb(*args, **kwargs):
        emb_called.append(True)
        return []

    monkeypatch.setattr(
        "memem.embedding_index._search_embedding_with_scores",
        _fake_emb,
        raising=False,
    )

    # Also stub FTS + ngram so we don't hit real I/O
    monkeypatch.setattr("memem.search_index._search_fts", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(
        "memem.obsidian_store._ngram_search_candidates", lambda *a, **kw: [], raising=False
    )

    from memem.recall import _search_memories_fts

    _search_memories_fts("test query", scope_id="default", limit=5)

    assert not emb_called, (
        "_search_embedding_with_scores was called despite MEMEM_USE_EMBEDDINGS=False"
    )


# ---------------------------------------------------------------------------
# Test 5: MEMEM_USE_EMBEDDINGS=1 (default) → embedding future IS submitted
# ---------------------------------------------------------------------------

def test_embeddings_enabled_submits_emb_future(monkeypatch):
    """When MEMEM_USE_EMBEDDINGS=1, _search_embedding_with_scores must be called."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", True)

    emb_called = []

    def _fake_emb(*args, **kwargs):
        emb_called.append(True)
        return []

    monkeypatch.setattr(
        "memem.embedding_index._search_embedding_with_scores",
        _fake_emb,
        raising=False,
    )

    monkeypatch.setattr("memem.search_index._search_fts", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(
        "memem.obsidian_store._ngram_search_candidates", lambda *a, **kw: [], raising=False
    )

    from memem.recall import _search_memories_fts

    _search_memories_fts("test query", scope_id="default", limit=5)

    assert emb_called, (
        "_search_embedding_with_scores was NOT called despite MEMEM_USE_EMBEDDINGS=True"
    )


# ---------------------------------------------------------------------------
# Test 6: _warmup_model logs "embedding judge enabled" when embeddings ON
# ---------------------------------------------------------------------------

def test_warmup_model_logs_enabled(monkeypatch, capfd):
    """_warmup_model must print/log 'embedding judge enabled' when MEMEM_USE_EMBEDDINGS=1."""
    import memem.settings as settings
    import memem.slice_daemon as daemon

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", True)

    # Stub out the actual embedding import so it doesn't need real model weights
    import sys
    import types

    fake_emb_mod = types.ModuleType("memem.embedding_index")
    monkeypatch.setitem(sys.modules, "memem.embedding_index", fake_emb_mod)

    daemon._warmup_model()

    captured = capfd.readouterr()
    assert "embedding judge enabled" in captured.out, (
        f"Expected 'embedding judge enabled' in stdout, got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: _warmup_model logs "embedding judge disabled" when embeddings OFF
# ---------------------------------------------------------------------------

def test_warmup_model_logs_disabled(monkeypatch, capfd):
    """_warmup_model must print 'embedding judge disabled' when MEMEM_USE_EMBEDDINGS=0."""
    import memem.settings as settings
    import memem.slice_daemon as daemon

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", False)

    daemon._warmup_model()

    captured = capfd.readouterr()
    assert "embedding judge disabled" in captured.out, (
        f"Expected 'embedding judge disabled' in stdout, got: {captured.out!r}"
    )
    assert "MEMEM_USE_EMBEDDINGS=0" in captured.out, (
        f"Expected 'MEMEM_USE_EMBEDDINGS=0' hint in stdout, got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: emb_score_by_id is empty dict when embeddings disabled (scoring path)
# ---------------------------------------------------------------------------

def test_fts_scoring_only_when_embeddings_disabled(monkeypatch):
    """When embeddings disabled, emb_score_by_id must be empty (no emb weight applied)."""
    import memem.settings as settings

    monkeypatch.setattr(settings, "MEMEM_USE_EMBEDDINGS", False)

    # Stub FTS to return one id, ngram empty, embedding not called
    monkeypatch.setattr(
        "memem.search_index._search_fts",
        lambda *a, **kw: ["abc123"],
        raising=False,
    )
    monkeypatch.setattr(
        "memem.obsidian_store._ngram_search_candidates",
        lambda *a, **kw: [],
        raising=False,
    )

    emb_called = []

    def _should_not_be_called(*args, **kwargs):
        emb_called.append(True)
        return []

    monkeypatch.setattr(
        "memem.embedding_index._search_embedding_with_scores",
        _should_not_be_called,
        raising=False,
    )

    # Stub _find_memory to return nothing — just checking no emb call
    monkeypatch.setattr(
        "memem.obsidian_store._find_memory",
        lambda mid: None,
        raising=False,
    )

    from memem.recall import _search_memories_fts

    _search_memories_fts("something", scope_id="default", limit=5)

    assert not emb_called, (
        "_search_embedding_with_scores was called despite MEMEM_USE_EMBEDDINGS=False"
    )
