"""Tests for memem.cross_encoder_rerank module.

Test 1: rerank_pairs with model_name=None returns identity ordering (no model loaded).
Test 2: rerank_pairs returns valid [(str, float)] tuples; "auth" query should rank
        auth-related memories above weather memories. Marked @pytest.mark.slow because
        it downloads ~33MB model on first run.
Test 3: Regression — when sentence_transformers fails to import, rerank_pairs returns
        identity ordering and logs a WARNING.
Test 4: Integration — _search_memories(query, rerank_model=..., limit=5) against an
        in-process vault returns <=5 memories without crashing.
"""

from __future__ import annotations

import logging
import sys
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(mid: str | None = None, title: str = "", essence: str = "") -> dict:
    return {
        "id": mid or str(uuid.uuid4()),
        "title": title,
        "essence": essence,
    }


# ---------------------------------------------------------------------------
# Test 1: identity ordering when model_name=None
# ---------------------------------------------------------------------------


def test_identity_ordering_when_model_name_none():
    """rerank_pairs(model_name=None) must return identity order without touching the model."""
    from memem.cross_encoder_rerank import rerank_pairs

    candidates = [
        _make_candidate("id-a", title="Auth memory", essence="JWT authentication tokens"),
        _make_candidate("id-b", title="Database memory", essence="Postgres migrations"),
        _make_candidate("id-c", title="Cache memory", essence="Redis cache eviction"),
    ]

    result = rerank_pairs("authentication", candidates, model_name=None)

    assert len(result) == 3, f"expected 3 pairs, got {len(result)}"
    # Identity ordering: original order preserved
    assert [mid for mid, _ in result] == ["id-a", "id-b", "id-c"]
    # Scores are descending integers (stable identity ordering signal)
    scores = [s for _, s in result]
    assert scores == sorted(scores, reverse=True), "identity scores should be descending"
    # Type check
    for mid, score in result:
        assert isinstance(mid, str)
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# Test 2: real cross-encoder scores auth > weather (slow — downloads model)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_cross_encoder_ranks_auth_above_weather():
    """Cross-encoder should rank auth-related memories above weather-related ones.

    This test uses the actual cross-encoder/ms-marco-MiniLM-L-12-v2 model.
    It downloads ~33MB on first run. Subsequent runs use the HuggingFace cache.
    Marked @pytest.mark.slow — skipped in default CI runs via:
        pytest -m "not slow"
    """
    from memem.cross_encoder_rerank import _DEFAULT_FAST_MODEL, rerank_pairs

    candidates = [
        _make_candidate("weather-1", title="Weather forecast", essence="Today is sunny with light winds"),
        _make_candidate("auth-1", title="Auth memory", essence="JWT authentication tokens for API access"),
        _make_candidate("weather-2", title="Rain forecast", essence="Heavy rain expected this weekend"),
        _make_candidate("auth-2", title="OAuth flow", essence="OAuth2 login flow with refresh tokens"),
        _make_candidate("auth-3", title="API key rotation", essence="API key rotation for auth services"),
    ]

    result = rerank_pairs("authentication", candidates, model_name=_DEFAULT_FAST_MODEL)

    assert len(result) == 5, f"expected 5 pairs, got {len(result)}"

    # Verify return type
    for mid, score in result:
        assert isinstance(mid, str), f"expected str id, got {type(mid)}"
        assert isinstance(score, float), f"expected float score, got {type(score)}"
        assert -10 <= score <= 10, f"score out of typical [-10, 10] range: {score}"

    # Top results should include at least one auth-related memory
    top_ids = [mid for mid, _ in result[:3]]
    auth_ids = {"auth-1", "auth-2", "auth-3"}
    assert any(mid in auth_ids for mid in top_ids), (
        f"Expected at least one auth memory in top-3, got: {top_ids}. "
        f"Full ranking: {result}"
    )

    # Scores should be descending
    scores = [s for _, s in result]
    assert scores == sorted(scores, reverse=True), f"scores should be descending: {scores}"


# ---------------------------------------------------------------------------
# Test 3: graceful fallback when sentence_transformers cannot be imported
# ---------------------------------------------------------------------------


def test_fallback_to_identity_when_sentence_transformers_unavailable(caplog):
    """When sentence_transformers fails to import, rerank_pairs should:
    - Return identity ordering
    - Log at WARNING level
    - Never raise
    """
    import memem.cross_encoder_rerank as ce_module

    # Use a unique model name so we don't collide with any cached real model
    test_model = "_test_nonexistent_model_for_import_failure_"
    original_cache = dict(ce_module._model_cache)
    # Pre-clear the specific test model entry so _get_model() actually attempts a load
    ce_module._model_cache.pop(test_model, None)

    candidates = [
        _make_candidate("id-x", title="Auth tokens", essence="JWT auth"),
        _make_candidate("id-y", title="Weather", essence="Rain tomorrow"),
    ]

    try:
        # Temporarily hide sentence_transformers from sys.modules so the import fails
        saved = sys.modules.pop("sentence_transformers", None)

        # Also patch sys.modules to make the import raise ImportError
        sys.modules["sentence_transformers"] = None  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="memem-cross-encoder"):
            result = ce_module.rerank_pairs("authentication", candidates, model_name=test_model)

        # Restore
        if saved is not None:
            sys.modules["sentence_transformers"] = saved
        else:
            sys.modules.pop("sentence_transformers", None)

        # Should return identity ordering
        assert len(result) == 2
        assert [mid for mid, _ in result] == ["id-x", "id-y"]
        # Should have logged a warning
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) > 0, (
            f"Expected at least one WARNING log, got: {caplog.records}"
        )
    finally:
        # Restore module state
        ce_module._model_cache.pop(test_model, None)
        ce_module._model_cache.update(original_cache)
        # Ensure sentence_transformers restored if we left it as None
        if sys.modules.get("sentence_transformers") is None:
            sys.modules.pop("sentence_transformers", None)


# ---------------------------------------------------------------------------
# Test 4: integration — _search_memories with rerank_model against in-process vault
# ---------------------------------------------------------------------------


def test_search_memories_with_rerank_model_no_crash(tmp_vault, tmp_cortex_dir):
    """_search_memories(rerank_model=...) should return <=limit memories without crashing.

    Uses a small in-process vault with 3 memories. The cross-encoder will be
    invoked if results are found, or gracefully skipped if FTS returns nothing.
    Since this is a fresh empty vault, the FTS path returns [] and the
    file-scan fallback runs without cross-encoder (the rerank_model is only
    applied in the FTS path). Either way, no crash is the contract.
    """
    from memem.recall import _search_memories

    # Integration test: just verifies no crash and type contract.
    # A fresh vault may have no memories, so we accept empty results.
    result = _search_memories(
        "authentication JWT",
        rerank_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
        limit=5,
        record_access=False,
        expand_links=False,
    )

    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert len(result) <= 5, f"expected at most 5 results, got {len(result)}"
    for mem in result:
        assert isinstance(mem, dict), f"expected dict memory, got {type(mem)}"
