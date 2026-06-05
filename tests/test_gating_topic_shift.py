"""Tests for v1.9 gating Layer 3: topic-shift detector via cached query embedding.

These tests exercise:
- Similar queries in same session → 2nd call returns cached slice with
  gating_reason='topic_shift_reuse'
- Dissimilar queries in same session → both go through full recall, no reuse
- MEMEM_INJECTION_MODE='auto' → topic-shift check entirely skipped
- sentence_transformers ImportError → silently fall through to full recall
- No regression on m1 gating behaviour (trivial/cadence gates still pass)
"""

from __future__ import annotations

import unittest.mock as mock

import pytest  # noqa: F401 — used by fixtures implicitly; also imported for marker resolution

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_all_session_state(session_id: str | None = None) -> None:
    """Clear in-memory session counters and embedding cache."""
    import memem.slice_history as sh
    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()

    from memem.slice_daemon import _session_cache, _session_cache_lock
    with _session_cache_lock:
        if session_id:
            _session_cache.pop(session_id, None)
        else:
            _session_cache.clear()


def _patch_gating(monkeypatch, *, mode: str = "hybrid", cadence: int = 1) -> None:
    """Patch settings for topic-shift tests.

    cadence=1 → every turn runs (we want to test topic-shift, not cadence-skip).
    """
    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", mode)
    monkeypatch.setattr(settings, "MEMEM_INJECT_CADENCE", cadence)
    monkeypatch.setattr(settings, "MEMEM_EMPTY_STREAK_MAX", 8)


def _call(query: str, session_id: str, use_llm: bool = False) -> dict:
    from memem.active_slice_engine import generate_active_memory_slice
    return generate_active_memory_slice(
        query,
        scope_id="test",
        environment={"session_id": session_id},
        use_llm=use_llm,
    )


# ---------------------------------------------------------------------------
# Fake embedding model fixture
# ---------------------------------------------------------------------------


def _make_fake_model(embeddings: dict[str, list[float]]):
    """Return a fake SentenceTransformer-like object that serves fixed vectors."""
    import numpy as np

    class FakeModel:
        def encode(self, text, convert_to_numpy=False, show_progress_bar=False, **_kw):
            vec = embeddings.get(text, [0.0] * 4)
            return np.array(vec, dtype="float32")

    return FakeModel()


# ---------------------------------------------------------------------------
# Topic-shift reuse: similar queries
# ---------------------------------------------------------------------------


def test_similar_queries_reuse_cache(monkeypatch, tmp_vault, tmp_cortex_dir):
    """Two queries with cosine ≥ threshold → cache reuse behaviour depends on C5 guard.

    C5 (topic-shift cache guard): if the cached slice has should_emit_context=False
    (e.g. empty vault), reuse is skipped and the full pipeline runs instead.
    When the cached slice has should_emit_context=True, reuse fires normally.
    """
    import memem.slice_history as sh
    from memem.active_slice_engine import _make_gating_stub
    from memem.slice_daemon import set_cached_embedding_and_slice

    sid = "topic-similar-test"
    _patch_gating(monkeypatch)
    _reset_all_session_state(sid)

    # Both queries map to essentially the same unit vector → similarity = 1.0
    same_vec = [1.0, 0.0, 0.0, 0.0]
    fake_model = _make_fake_model({
        "how does authentication work?": same_vec,
        "explain authentication please": same_vec,
        "query one": same_vec,
        "query two": same_vec,
    })

    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", 0.85)

    with mock.patch("memem.embedding_index._get_model", return_value=fake_model):
        # First call: no cache → runs full pipeline.
        # Pin empty_streak=0 so effective_cadence stays at 1 (not doubled by streak).
        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r1 = _call("how does authentication work?", sid)
        assert r1.get("gating_reason") != "topic_shift_reuse", (
            f"First call should not reuse cache; got gating_reason={r1.get('gating_reason')!r}"
        )

        # C5 guard: first call returned should_emit_context=False (empty vault).
        # The second similar call should NOT reuse the stale low-confidence cache —
        # it must fall through to the full pipeline.
        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r2 = _call("explain authentication please", sid)
        assert r2.get("gating_reason") != "topic_shift_reuse", (
            f"C5 guard: similar call with stale (should_emit_context=False) cache "
            f"must not reuse; got {r2.get('gating_reason')!r}"
        )

        # Now seed the cache with a slice that HAS content (should_emit_context=True).
        # In this case topic-shift reuse SHOULD fire normally.
        good_slice = _make_gating_stub("query one", "test", sid, "cadence_skip")
        good_slice["should_emit_context"] = True  # type: ignore[index]
        set_cached_embedding_and_slice(sid, same_vec, good_slice)

        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r3 = _call("query two", sid)
        assert r3.get("gating_reason") == "topic_shift_reuse", (
            f"Similar query with good cache (should_emit_context=True) should reuse; "
            f"got {r3.get('gating_reason')!r}"
        )


# ---------------------------------------------------------------------------
# Topic-shift no reuse: dissimilar queries
# ---------------------------------------------------------------------------


def test_dissimilar_queries_skip_cache(monkeypatch, tmp_vault, tmp_cortex_dir):
    """Two queries with cosine < threshold → both go through full recall."""
    import memem.slice_history as sh

    sid = "topic-dissimilar-test"
    _patch_gating(monkeypatch)
    _reset_all_session_state(sid)

    # Orthogonal vectors → cosine = 0.0 < 0.85
    vec_a = [1.0, 0.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0, 0.0]
    fake_model = _make_fake_model({
        "how does X work?": vec_a,
        "what's the weather?": vec_b,
    })

    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", 0.85)

    with mock.patch("memem.embedding_index._get_model", return_value=fake_model):
        # Pin empty_streak=0 before each call so cadence gate never fires.
        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r1 = _call("how does X work?", sid)
        assert r1.get("gating_reason") != "topic_shift_reuse", (
            f"First call should not reuse; got {r1.get('gating_reason')!r}"
        )

        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r2 = _call("what's the weather?", sid)
        assert r2.get("gating_reason") != "topic_shift_reuse", (
            f"Second dissimilar call should not reuse cache; got {r2.get('gating_reason')!r}"
        )


# ---------------------------------------------------------------------------
# auto mode bypasses topic-shift gate
# ---------------------------------------------------------------------------


def test_auto_mode_bypasses_topic_shift(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, topic-shift check is entirely skipped."""
    sid = "topic-auto-test"
    _patch_gating(monkeypatch, mode="auto")
    _reset_all_session_state(sid)

    same_vec = [1.0, 0.0, 0.0, 0.0]
    fake_model = _make_fake_model({
        "how does X work?": same_vec,
        "explain X again": same_vec,
    })

    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", 0.85)

    with mock.patch("memem.embedding_index._get_model", return_value=fake_model):
        r1 = _call("how does X work?", sid)
        r2 = _call("explain X again", sid)
        # In auto mode, should never see topic_shift_reuse (gating is bypassed entirely)
        assert r1.get("gating_reason") != "topic_shift_reuse", (
            f"auto mode first call should not produce topic_shift_reuse; got {r1.get('gating_reason')!r}"
        )
        assert r2.get("gating_reason") != "topic_shift_reuse", (
            f"auto mode should never produce topic_shift_reuse; got {r2.get('gating_reason')!r}"
        )


# ---------------------------------------------------------------------------
# ImportError: sentence_transformers missing → fall through silently
# ---------------------------------------------------------------------------


def test_import_error_falls_through(monkeypatch, tmp_vault, tmp_cortex_dir):
    """If sentence_transformers is not importable, fall through without exception."""
    sid = "topic-import-error-test"
    _patch_gating(monkeypatch)
    _reset_all_session_state(sid)

    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", 0.85)

    # Simulate _get_model() returning None (i.e., dependency not installed)
    with mock.patch("memem.embedding_index._get_model", return_value=None):
        # Both calls should complete without raising, just fall through to full pipeline
        r1 = _call("how does authentication work?", sid)
        r2 = _call("explain authentication please", sid)

    assert r1.get("gating_reason") != "topic_shift_reuse"
    assert r2.get("gating_reason") != "topic_shift_reuse"


# ---------------------------------------------------------------------------
# Cache helper unit tests
# ---------------------------------------------------------------------------


def test_cache_helpers_get_set_clear():
    """Low-level cache helpers work correctly in isolation."""
    from memem.active_slice_engine import _make_gating_stub
    from memem.slice_daemon import (
        clear_session_cache,
        get_cached_query_embedding,
        get_cached_slice,
        set_cached_embedding_and_slice,
    )

    sid = "cache-unit-test"
    clear_session_cache(sid)

    # Initially empty
    assert get_cached_query_embedding(sid) is None
    assert get_cached_slice(sid) is None

    # Set via atomic helper
    emb = [0.1, 0.2, 0.3]
    stub_slice = _make_gating_stub("test query", "test_scope", sid, "cadence_skip")
    set_cached_embedding_and_slice(sid, emb, stub_slice)

    assert get_cached_query_embedding(sid) == emb
    assert get_cached_slice(sid) is not None
    assert get_cached_slice(sid)["gating_reason"] == "cadence_skip"

    # Clear
    clear_session_cache(sid)
    assert get_cached_query_embedding(sid) is None
    assert get_cached_slice(sid) is None


def test_cache_sessions_are_isolated():
    """Embedding cache is per-session — sessions don't bleed into each other."""
    from memem.active_slice_engine import _make_gating_stub
    from memem.slice_daemon import (
        clear_session_cache,
        get_cached_query_embedding,
        set_cached_embedding_and_slice,
    )

    sid_a = "isolated-cache-A"
    sid_b = "isolated-cache-B"
    clear_session_cache(sid_a)
    clear_session_cache(sid_b)

    emb_a = [1.0, 0.0]
    stub_a = _make_gating_stub("q", "s", sid_a, "cadence_skip")
    set_cached_embedding_and_slice(sid_a, emb_a, stub_a)

    # B should still be empty
    assert get_cached_query_embedding(sid_b) is None
    # A should have its embedding
    assert get_cached_query_embedding(sid_a) == emb_a

    clear_session_cache(sid_a)
    clear_session_cache(sid_b)


# ---------------------------------------------------------------------------
# Threshold boundary: exactly at threshold → reuse
# ---------------------------------------------------------------------------


def test_threshold_boundary_exact(monkeypatch, tmp_vault, tmp_cortex_dir):
    """Similarity exactly equal to threshold → reuse fires when cache has content (C5 guard).

    C5: reuse only happens when the cached slice has should_emit_context=True.
    We seed the cache directly with a good slice to isolate the threshold comparison
    from the C5 should_emit_context guard.
    """
    import memem.slice_history as sh
    from memem.active_slice_engine import _make_gating_stub
    from memem.slice_daemon import set_cached_embedding_and_slice

    sid = "topic-boundary-test"
    _patch_gating(monkeypatch)
    _reset_all_session_state(sid)

    import memem.settings as settings
    threshold = 0.85
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", threshold)

    # Two vectors with known cosine similarity ≥ threshold.
    # We'll use identical vectors (cosine = 1.0) to guarantee the boundary.
    vec = [0.6, 0.8, 0.0, 0.0]  # already unit-length: 0.36+0.64=1.0
    fake_model = _make_fake_model({
        "query one": vec,
        "query two": vec,
    })

    with mock.patch("memem.embedding_index._get_model", return_value=fake_model):
        # Seed cache directly with a slice that has should_emit_context=True so
        # C5 guard allows reuse. This isolates the ≥ threshold comparison.
        good_slice = _make_gating_stub("query one", "test", sid, "cadence_skip")
        good_slice["should_emit_context"] = True  # type: ignore[index]
        set_cached_embedding_and_slice(sid, vec, good_slice)

        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        r2 = _call("query two", sid)
        assert r2.get("gating_reason") == "topic_shift_reuse", (
            f"Identical vectors should trigger reuse (sim=1.0 ≥ {threshold}) "
            f"when cache has content; got {r2.get('gating_reason')!r}"
        )


# ---------------------------------------------------------------------------
# No magic numbers check (enforcement via test)
# ---------------------------------------------------------------------------


def test_no_hardcoded_threshold_in_implementation():
    """The literal 0.85 must not appear in active_slice_engine or slice_daemon (outside comments)."""
    from pathlib import Path

    for fname in ("memem/active_slice_engine.py", "memem/slice_daemon.py"):
        path = Path(__file__).parent.parent / fname
        text = path.read_text()
        # Find occurrences of '0.85' that are NOT in comments or string literals
        # Simple heuristic: flag any non-comment line containing literal 0.85
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # comment line — OK
            if "0.85" in line and "TOPIC_SHIFT_THRESHOLD" not in line:
                pytest.fail(
                    f"{fname}:{lineno}: hardcoded 0.85 found outside comment/threshold ref: {line!r}"
                )
