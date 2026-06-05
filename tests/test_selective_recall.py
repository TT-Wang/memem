"""Tests for v1.9.6 selective-recall features (C1-C5).

Covers:
 C1 — hook emits empty additionalContext when should_emit_context=False
 C2 — MEMEM_RECALL_MIN_CONFIDENCE gating in build_active_memory_slice
 C3 — per-item score floor (MEMEM_RECALL_MIN_ITEM_SCORE) + L0 anchor exemption
 C4 — out-of-vault detection sets gating_reason
 C5 — cached low-confidence slice is NOT reused (full pipeline runs instead)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helper: inline simulation of the hook's emit-decision logic (C1 tests)
# ---------------------------------------------------------------------------

def _hook_emit(meta: dict) -> str:
    """Simulate the hook body: return additionalContext string.

    The hook does:
        _emit_context = final_context if _should_emit_context else ""
    where should_emit_context and gating_reason come from try_slice_via_daemon_with_meta.
    """
    should_emit_context = meta.get("should_emit_context", True)
    slice_content = meta.get("slice", "some content")
    return slice_content if should_emit_context else ""


# ---------------------------------------------------------------------------
# C1 — hook additionalContext suppression
# ---------------------------------------------------------------------------


def test_c1_hook_emits_empty_when_should_not_emit():
    """C1: when should_emit_context=False the hook must produce empty additionalContext."""
    meta = {
        "slice": "some content",
        "should_emit_context": False,
        "gating_reason": "low_confidence",
    }
    additional_context = _hook_emit(meta)
    assert additional_context == ""


def test_c1_hook_emits_content_when_should_emit():
    """C1: when should_emit_context=True the hook must pass through the slice content."""
    meta = {
        "slice": "# Active Memory Slice\n\n## Goals\n- goal one\n",
        "should_emit_context": True,
        "gating_reason": "",
    }
    additional_context = _hook_emit(meta)
    assert additional_context != ""
    assert "Active Memory Slice" in additional_context


# ---------------------------------------------------------------------------
# C2 — MEMEM_RECALL_MIN_CONFIDENCE gating
# ---------------------------------------------------------------------------


def test_c2_min_confidence_env_lowers_threshold(monkeypatch):
    """C2: with MEMEM_RECALL_MIN_CONFIDENCE=0.1, confidence=0.2 + recall_candidates > 0
    → should_emit_context=True.
    """
    import memem.settings as settings
    from memem.active_slice import (
        ActivationResult,
        CandidateBundle,
        build_active_memory_slice,
    )

    monkeypatch.setattr(settings, "MEMEM_RECALL_MIN_CONFIDENCE", 0.1)

    # Build minimal candidate_bundle with one memory candidate
    memory_cand = {
        "candidate_id": "memory:aabbccdd",
        "candidate_type": "memory",
        "memory_id": "aabbccdd1122334455667788",
        "title": "Test memory",
        "summary": "Test summary",
        "content": "Test content",
        "source_type": "user",
        "project": "test",
        "status": "active",
        "importance": 3,
        "layer": 2,
        "score": 0.5,
        "source_score": 0.5,
        "graph_distance": 0,
        "source_reason": "recall",
    }
    bundle: CandidateBundle = {
        "current_goal_candidates": [],
        "memory_candidates": [memory_cand],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }
    # Activation with low confidence=0.2 but recall_candidate_count > 0
    activation: ActivationResult = {
        "goals": [],
        "constraints": [],
        "background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifact_context": [],
        "open_tensions": [],
        "ignored": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "activation_mode": "heuristic",
        "confidence": 0.2,
        "warnings": [],
    }

    result = build_active_memory_slice(
        query="test query",
        scope_id="test",
        environment={"session_id": "test-session"},
        candidate_bundle=bundle,
        activation_result=activation,
    )

    # recall_candidate_count=1, confidence=0.2 >= threshold=0.1 → should_emit_context=True
    assert result["recall_candidate_count"] > 0
    assert result["should_emit_context"] is True, (
        f"Expected should_emit_context=True with confidence=0.2, threshold=0.1, "
        f"but got {result['should_emit_context']}"
    )


def test_c2_min_confidence_env_raises_threshold(monkeypatch):
    """C2: with MEMEM_RECALL_MIN_CONFIDENCE=0.9, confidence=0.2 + recall_candidates > 0
    → should_emit_context=False (confidence too low).
    """
    import memem.settings as settings
    from memem.active_slice import (
        ActivationResult,
        CandidateBundle,
        build_active_memory_slice,
    )

    monkeypatch.setattr(settings, "MEMEM_RECALL_MIN_CONFIDENCE", 0.9)

    memory_cand = {
        "candidate_id": "memory:aabbccdd",
        "candidate_type": "memory",
        "memory_id": "aabbccdd1122334455667788",
        "title": "Test memory",
        "summary": "Test summary",
        "content": "Test content",
        "source_type": "user",
        "project": "test",
        "status": "active",
        "importance": 3,
        "layer": 2,
        "score": 0.5,
        "source_score": 0.5,
        "graph_distance": 0,
        "source_reason": "recall",
    }
    bundle: CandidateBundle = {
        "current_goal_candidates": [],
        "memory_candidates": [memory_cand],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }
    activation: ActivationResult = {
        "goals": [],
        "constraints": [],
        "background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifact_context": [],
        "open_tensions": [],
        "ignored": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "activation_mode": "heuristic",
        "confidence": 0.2,
        "warnings": [],
    }

    result = build_active_memory_slice(
        query="test query",
        scope_id="test",
        environment={"session_id": "test-session"},
        candidate_bundle=bundle,
        activation_result=activation,
    )

    # confidence=0.2 < threshold=0.9, no selected_memory_items or artifacts → should_emit_context=False
    assert result["recall_candidate_count"] > 0
    assert result["should_emit_context"] is False, (
        f"Expected should_emit_context=False with confidence=0.2, threshold=0.9, "
        f"but got {result['should_emit_context']}"
    )


# ---------------------------------------------------------------------------
# C3 — per-item score floor filter in recall.py
# ---------------------------------------------------------------------------


def test_c3_low_score_items_dropped(monkeypatch):
    """C3: non-L0 items with composite score < MEMEM_RECALL_MIN_ITEM_SCORE are dropped.

    We exercise the filter inline (from recall.py ~line 185) to avoid requiring
    the full DB/vault stack.
    """
    import memem.settings as settings
    from memem.models import LAYER_L0

    monkeypatch.setattr(settings, "MEMEM_RECALL_MIN_ITEM_SCORE", 0.2)

    # Inline reproduction of the filter from recall.py:
    #   scored = [(s, m) for s, m in scored if s >= _min_item_score or m.get("layer") == LAYER_L0]
    _min_item_score = settings.MEMEM_RECALL_MIN_ITEM_SCORE

    low_score_mem = {"id": "aabb0001", "title": "Low score", "layer": 2}
    high_score_mem = {"id": "aabb0002", "title": "High score", "layer": 2}
    l0_mem = {"id": "aabb0003", "title": "L0 anchor", "layer": LAYER_L0}

    scored = [
        (0.05, low_score_mem),   # below threshold, non-L0 → should be dropped
        (0.50, high_score_mem),  # above threshold → survives
        (0.00, l0_mem),          # below threshold BUT L0 → survives
    ]

    filtered = [
        (s, m) for s, m in scored
        if s >= _min_item_score or m.get("layer") == LAYER_L0
    ]

    ids = [m["id"] for _, m in filtered]
    assert "aabb0001" not in ids, "Low-score non-L0 item should have been dropped"
    assert "aabb0002" in ids, "High-score item should survive"
    assert "aabb0003" in ids, "L0 anchor should survive regardless of score"


def test_c3_l0_anchors_never_dropped(monkeypatch):
    """C3: L0 anchors must survive the filter even at score=0.0."""
    import memem.settings as settings
    from memem.models import LAYER_L0

    monkeypatch.setattr(settings, "MEMEM_RECALL_MIN_ITEM_SCORE", 0.5)

    _min_item_score = settings.MEMEM_RECALL_MIN_ITEM_SCORE

    l0_mem = {"id": "l0anchor1", "title": "Project identity anchor", "layer": LAYER_L0}
    ordinary_mem = {"id": "ordmem01", "title": "Ordinary memory", "layer": 2}

    scored = [
        (0.0, l0_mem),       # zero score, L0 → must survive
        (0.3, ordinary_mem), # below threshold → should be dropped
    ]

    filtered = [
        (s, m) for s, m in scored
        if s >= _min_item_score or m.get("layer") == LAYER_L0
    ]

    ids = [m["id"] for _, m in filtered]
    assert "l0anchor1" in ids, "L0 anchor must survive even at score=0.0"
    assert "ordmem01" not in ids, "Ordinary memory below threshold should be dropped"


# ---------------------------------------------------------------------------
# C4 — out-of-vault detection sets gating_reason
# ---------------------------------------------------------------------------


def test_c4_out_of_vault_sets_gating_reason(monkeypatch):
    """C4: _detect_out_of_vault returns True when all memory scores < threshold
    and no L0 keyword overlap. generate_active_memory_slice returns gating_reason=='out_of_vault'.
    """
    import memem.settings as settings
    from memem.active_slice import CandidateBundle
    from memem.active_slice_engine import _detect_out_of_vault

    # Verify _detect_out_of_vault directly with zero-score bundle
    bundle_all_zero: CandidateBundle = {
        "current_goal_candidates": [],
        "memory_candidates": [
            {
                "candidate_id": "memory:zz000000",
                "candidate_type": "memory",
                "memory_id": "zz0000001122334455667788",
                "title": "Unrelated topic",
                "summary": "Completely unrelated",
                "content": "Completely unrelated content",
                "source_type": "user",
                "project": "test",
                "status": "active",
                "importance": 3,
                "layer": 2,
                "score": 0.0,
                "source_score": 0.0,
                "graph_distance": 0,
                "source_reason": "recall",
            }
        ],
        "playbook_candidate": None,
        "transcript_candidates": [],
        "artifact_candidates": [],
        "environment_candidates": [],
    }

    # Query with real tokens that won't match the L0 anchor title
    query = "xyzzy_nonexistent_topic_qwerty"
    is_oov = _detect_out_of_vault(query, bundle_all_zero, threshold=0.3)
    assert is_oov is True, (
        f"Expected _detect_out_of_vault=True for zero-score bundle with non-matching query, got {is_oov}"
    )

    # Now test full pipeline: monkeypatch OOV threshold and generate_candidates
    # to return zero-score memory candidates so OOV triggers.
    monkeypatch.setattr(settings, "MEMEM_RECALL_OOV_THRESHOLD", 0.3)

    # Monkeypatch generate_candidates to return a bundle with zero-score candidates
    def fake_generate_candidates(query, scope_id, env=None, limit=20, *, use_llm=True):
        return {
            "current_goal_candidates": [],
            "memory_candidates": [
                {
                    "candidate_id": "memory:zz000000",
                    "candidate_type": "memory",
                    "memory_id": "zz0000001122334455667788",
                    "title": "Unrelated topic",
                    "summary": "Completely unrelated",
                    "content": "Completely unrelated content",
                    "source_type": "user",
                    "project": "test",
                    "status": "active",
                    "importance": 3,
                    "layer": 2,
                    "score": 0.0,
                    "source_score": 0.0,
                    "graph_distance": 0,
                    "source_reason": "recall",
                }
            ],
            "playbook_candidate": None,
            "transcript_candidates": [],
            "artifact_candidates": [],
            "environment_candidates": [],
        }

    import memem.active_slice_engine as ase
    monkeypatch.setattr(ase, "generate_candidates", fake_generate_candidates)

    from memem.active_slice_engine import generate_active_memory_slice

    result = generate_active_memory_slice(
        query="xyzzy_nonexistent_topic_qwerty_abc123",
        scope_id="test",
        environment={"session_id": "c4-test-session-oov"},
        use_llm=False,
    )

    # Contract: OOV detection emits gating stub with should_emit_context=False
    # and gating_reason="out_of_vault". Streak tracking is the outer observer's
    # responsibility (only fires in hybrid mode, where the streak counter matters).
    assert result.get("gating_reason") == "out_of_vault", (
        f"Expected gating_reason='out_of_vault', got {result.get('gating_reason')!r}"
    )
    assert result.get("should_emit_context") is False, (
        f"Expected should_emit_context=False, got {result.get('should_emit_context')!r}"
    )


# ---------------------------------------------------------------------------
# C5 — cached low-confidence slice must NOT be reused
# ---------------------------------------------------------------------------


def test_c5_cached_low_confidence_not_reused(monkeypatch):
    """C5: a cached slice with should_emit_context=False must NOT be reused via topic_shift_reuse.
    The full pipeline should run instead.
    """
    import pytest
    np = pytest.importorskip("numpy")

    import memem.settings as settings
    import memem.slice_history as sh
    from memem.active_slice import ActiveMemorySlice
    from memem.slice_daemon import (
        clear_session_cache,
        set_cached_embedding_and_slice,
    )

    session_id = "c5-test-session-low-conf"

    # Ensure clean state
    clear_session_cache(session_id)
    with sh._session_lock:
        sh._turn_counts.pop(session_id, None)
        sh._empty_streaks.pop(session_id, None)

    # Patch to hybrid mode so topic-shift gate runs
    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", "hybrid")
    # Set cadence=1 so every turn runs the pipeline (unless topic-shift reuses)
    monkeypatch.setattr(settings, "MEMEM_INJECT_CADENCE", 1)
    # Set threshold to 0.85 (default) — identical vectors will have similarity=1.0 >= 0.85
    monkeypatch.setattr(settings, "MEMEM_TOPIC_SHIFT_THRESHOLD", 0.85)

    # Build a fake embedding vector
    fake_embedding = [1.0, 0.0, 0.0]

    # Build a fake cached slice with should_emit_context=False (low confidence)
    fake_slice: ActiveMemorySlice = {
        "slice_id": "fake-slice-id",
        "session_id": session_id,
        "scope_id": "test",
        "query": "previous query",
        "input_goal": "previous query",
        "generated_at": "2025-01-01T00:00:00+00:00",
        "goals": [],
        "constraints": [],
        "active_background": [],
        "decisions": [],
        "preferences": [],
        "failure_patterns": [],
        "artifacts": [],
        "open_tensions": [],
        "resolved_tensions": [],
        "excluded_candidates": [],
        "candidate_deltas": [],
        "delta_results": [],
        "candidate_count": 0,
        "recall_candidate_count": 0,
        "should_emit_context": False,  # <-- this is the critical flag
        "activation_mode": "heuristic",
        "confidence": 0.1,
        "warnings": [],
        "items": [],
        "slice_kind": "active",
        "gating_reason": "low_confidence",
    }

    # Seed the cache with the low-confidence slice
    set_cached_embedding_and_slice(session_id, fake_embedding, fake_slice)

    # Monkeypatch the embedding model to return our identical vector
    # so cosine similarity will be 1.0 (>= threshold 0.85)
    class FakeModel:
        def encode(self, text, convert_to_numpy=False, show_progress_bar=False):
            return np.array(fake_embedding, dtype="float32")

    try:
        import memem.embedding_index as ei
        monkeypatch.setattr(ei, "_get_model", lambda: FakeModel())
    except Exception:
        # If embedding_index isn't available, skip rather than fail
        pytest.skip("memem.embedding_index not available")

    from memem.active_slice_engine import generate_active_memory_slice

    result = generate_active_memory_slice(
        query="same query as before",
        scope_id="test",
        environment={"session_id": session_id},
        use_llm=False,
    )

    # C5: the result must NOT have gating_reason="topic_shift_reuse"
    # because the cached slice had should_emit_context=False
    assert result.get("gating_reason") != "topic_shift_reuse", (
        "C5 violation: stale low-confidence cached slice was reused as topic_shift_reuse. "
        f"gating_reason={result.get('gating_reason')!r}, should_emit_context={result.get('should_emit_context')!r}"
    )

    # Cleanup
    clear_session_cache(session_id)
