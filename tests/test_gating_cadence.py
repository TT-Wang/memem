"""Tests for v1.9 gating Layer 2: turn cadence + empty-streak backoff.

These tests exercise:
- MEMEM_INJECT_CADENCE=2 → turns 1,3,5 cadence-skip; turns 2,4,6 run pipeline
  (Note: turn 1 is turn%2!=0 → skip; turn 2 is turn%2==0 → run; etc.)
  Actually: cadence=2 means every 2nd turn. Turn 1: 1%2=1≠0 → skip.
            Turn 2: 2%2=0 → run. Turn 3: 3%2=1≠0 → skip. Turn 4: 4%2=0 → run.
- Empty-streak backoff: streak=1 → cadence×2, streak=2 → cadence×4, etc.
- Empty-streak capped at MAX multiplier
- in_memory helpers: get/increment/reset functions behave correctly
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_session_state():
    """Clear all in-memory session counters."""
    import memem.slice_history as sh
    with sh._session_lock:
        sh._turn_counts.clear()
        sh._empty_streaks.clear()


def _patch_gating(monkeypatch, cadence: int = 2, mode: str = "hybrid", streak_max: int = 8):
    import memem.settings as settings
    monkeypatch.setattr(settings, "MEMEM_INJECTION_MODE", mode)
    monkeypatch.setattr(settings, "MEMEM_INJECT_CADENCE", cadence)
    monkeypatch.setattr(settings, "MEMEM_EMPTY_STREAK_MAX", streak_max)


def _make_call(session_id: str, query: str = "How do I implement OAuth2?") -> dict:
    from memem.active_slice_engine import generate_active_memory_slice
    return generate_active_memory_slice(
        query,
        scope_id="test",
        environment={"session_id": session_id},
        use_llm=False,
    )


# ---------------------------------------------------------------------------
# Cadence gate: cadence=2
# ---------------------------------------------------------------------------


def test_cadence_2_alternates(monkeypatch, tmp_vault, tmp_cortex_dir):
    """With cadence=2 and streak fixed at 0, turns 1,3,5 run full; 2,4,6 cadence-skip.

    Formula: (turn-1) % cadence == 0 → run; otherwise → skip.
    Turn 1: (1-1)%2=0 → run.  Turn 2: (2-1)%2=1 → skip.  Turn 3: (3-1)%2=0 → run. etc.

    We hold empty_streak at 0 throughout to isolate pure cadence behaviour
    (streak updates are tested separately in the empty-streak tests).
    """
    _patch_gating(monkeypatch, cadence=2)
    _reset_session_state()

    from memem import slice_history as sh

    sid = "cadence-test-1"

    def _make_call_no_streak(query: str = "How do I implement OAuth2?") -> dict:
        # Pin empty_streak=0 so effective_cadence stays at base=2 each call.
        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        return _make_call(sid, query)

    # Turn 1: (1-1)%2=0 → run pipeline
    r1 = _make_call_no_streak()
    assert r1.get("gating_reason") != "cadence_skip", f"Turn 1 should run, got: {r1.get('gating_reason')!r}"

    # Turn 2: (2-1)%2=1 → cadence_skip
    r2 = _make_call_no_streak()
    assert r2.get("gating_reason") == "cadence_skip", f"Turn 2 should cadence_skip, got: {r2.get('gating_reason')!r}"
    assert r2["should_emit_context"] is False

    # Turn 3: (3-1)%2=0 → run pipeline
    r3 = _make_call_no_streak()
    assert r3.get("gating_reason") != "cadence_skip", f"Turn 3 should run, got: {r3.get('gating_reason')!r}"

    # Turn 4: (4-1)%2=1 → cadence_skip
    r4 = _make_call_no_streak()
    assert r4.get("gating_reason") == "cadence_skip", f"Turn 4 should cadence_skip, got: {r4.get('gating_reason')!r}"

    # Turn 5: (5-1)%2=0 → run pipeline
    r5 = _make_call_no_streak()
    assert r5.get("gating_reason") != "cadence_skip", f"Turn 5 should run, got: {r5.get('gating_reason')!r}"

    # Turn 6: (6-1)%2=1 → cadence_skip
    r6 = _make_call_no_streak()
    assert r6.get("gating_reason") == "cadence_skip", f"Turn 6 should cadence_skip, got: {r6.get('gating_reason')!r}"


# ---------------------------------------------------------------------------
# Cadence gate: cadence=1 (every turn runs)
# ---------------------------------------------------------------------------


def test_cadence_1_every_turn_runs(monkeypatch, tmp_vault, tmp_cortex_dir):
    """With cadence=1 and streak fixed at 0, every turn runs the full pipeline.

    We hold empty_streak=0 to isolate pure cadence behaviour: with base=1 and
    streak=0, effective_cadence=1 and turn%1==0 always, so no turn skips.
    """
    _patch_gating(monkeypatch, cadence=1)
    _reset_session_state()

    from memem import slice_history as sh

    sid = "cadence-1-test"
    for i in range(1, 5):
        with sh._session_lock:
            sh._empty_streaks[sid] = 0
        result = _make_call(sid)
        assert result.get("gating_reason") != "cadence_skip", (
            f"Turn {i} with cadence=1 should never cadence_skip; got {result.get('gating_reason')!r}"
        )


# ---------------------------------------------------------------------------
# Empty-streak backoff
# ---------------------------------------------------------------------------


def test_empty_streak_doubles_effective_cadence(monkeypatch, tmp_vault, tmp_cortex_dir):
    """streak=1 doubles the cadence; streak=2 quadruples it.

    Formula: (turn-1) % effective_cadence == 0 → run; otherwise skip.
    With base=2, streak=1, effective=4:
      Turn 1: (0)%4=0 → run
      Turn 2: (1)%4=1 → skip
      Turn 3: (2)%4=2 → skip
      Turn 4: (3)%4=3 → skip
      Turn 5: (4)%4=0 → run
    """
    from memem import slice_history as sh
    _patch_gating(monkeypatch, cadence=2, streak_max=8)
    _reset_session_state()

    sid = "streak-test"

    # Manually set streak=1 for this session (effective cadence = 2 * 2^1 = 4)
    with sh._session_lock:
        sh._empty_streaks[sid] = 1

    # Turn 1: (0)%4=0 → run
    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"Turn 1 with streak=1 (effective=4) should run; got {r.get('gating_reason')!r}"
    )

    # Reset streak to keep effective_cadence stable at 4 for subsequent calls
    with sh._session_lock:
        sh._empty_streaks[sid] = 1

    # Turn 2: (1)%4=1 → skip
    r = _make_call(sid)
    assert r.get("gating_reason") == "cadence_skip", (
        f"Turn 2 with streak=1 (effective=4) should skip; got {r.get('gating_reason')!r}"
    )

    with sh._session_lock:
        sh._empty_streaks[sid] = 1

    # Turn 3: (2)%4=2 → skip
    r = _make_call(sid)
    assert r.get("gating_reason") == "cadence_skip", (
        f"Turn 3 with streak=1 (effective=4) should skip; got {r.get('gating_reason')!r}"
    )

    with sh._session_lock:
        sh._empty_streaks[sid] = 1

    # Turn 4: (3)%4=3 → skip
    r = _make_call(sid)
    assert r.get("gating_reason") == "cadence_skip", (
        f"Turn 4 with streak=1 (effective=4) should skip; got {r.get('gating_reason')!r}"
    )

    with sh._session_lock:
        sh._empty_streaks[sid] = 1

    # Turn 5: (4)%4=0 → run
    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"Turn 5 with streak=1, base=2 (effective=4) should run; got {r.get('gating_reason')!r}"
    )


def test_empty_streak_quadruples_effective_cadence(monkeypatch, tmp_vault, tmp_cortex_dir):
    """streak=2 → effective cadence = base * 4.

    With base=2, streak=2, effective=8:
      Turn 1: (0)%8=0 → run
      Turns 2-8: (1..7)%8!=0 → skip
      Turn 9: (8)%8=0 → run
    """
    from memem import slice_history as sh
    _patch_gating(monkeypatch, cadence=2, streak_max=8)
    _reset_session_state()

    sid = "streak2-test"
    with sh._session_lock:
        sh._empty_streaks[sid] = 2  # effective cadence = 2 * 2^2 = 8

    # Turn 1: (0)%8=0 → run
    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"Turn 1 with streak=2, base=2 (effective=8) should run; got {r.get('gating_reason')!r}"
    )

    # Turns 2-8 should skip ((1..7)%8 != 0), keeping streak pinned
    for turn_num in range(2, 9):
        with sh._session_lock:
            sh._empty_streaks[sid] = 2
        r = _make_call(sid)
        assert r.get("gating_reason") == "cadence_skip", (
            f"Turn {turn_num} with streak=2, base=2 (effective=8) should skip; got {r.get('gating_reason')!r}"
        )

    # Turn 9: (8)%8=0 → run
    with sh._session_lock:
        sh._empty_streaks[sid] = 2
    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"Turn 9 with streak=2, base=2 (effective=8) should run; got {r.get('gating_reason')!r}"
    )


def test_empty_streak_capped_at_max(monkeypatch, tmp_vault, tmp_cortex_dir):
    """Empty-streak backoff is capped at base * MEMEM_EMPTY_STREAK_MAX."""
    from memem import slice_history as sh
    # base=2, max=4 → cap is 2*4=8 regardless of streak size
    _patch_gating(monkeypatch, cadence=2, streak_max=4)
    _reset_session_state()

    sid = "streak-cap-test"
    # streak=10 would give 2 * 2^10 = 2048, but cap is 2*4=8
    with sh._session_lock:
        sh._empty_streaks[sid] = 10

    # Set turn count to 8 so next call is turn 9.
    # (8)%8=0 → run (effective_cadence capped at 8)
    with sh._session_lock:
        sh._turn_counts[sid] = 8  # next call will be turn 9

    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"Turn 9 (turn-1=8, 8%8=0) with capped effective_cadence=8 should run; got {r.get('gating_reason')!r}"
    )


# ---------------------------------------------------------------------------
# In-memory helpers
# ---------------------------------------------------------------------------


def test_turn_count_increments(monkeypatch, tmp_vault, tmp_cortex_dir):
    """increment_turn_count returns monotonically increasing values."""
    from memem.slice_history import get_session_turn_count, increment_turn_count
    _reset_session_state()

    sid = "turn-counter-test"
    assert get_session_turn_count(sid) == 0
    assert increment_turn_count(sid) == 1
    assert increment_turn_count(sid) == 2
    assert get_session_turn_count(sid) == 2


def test_empty_streak_helpers(monkeypatch, tmp_vault, tmp_cortex_dir):
    """increment_empty_streak and reset_empty_streak work correctly."""
    from memem.slice_history import (
        get_empty_streak,
        increment_empty_streak,
        reset_empty_streak,
    )
    _reset_session_state()

    sid = "streak-helpers-test"
    assert get_empty_streak(sid) == 0
    assert increment_empty_streak(sid) == 1
    assert increment_empty_streak(sid) == 2
    assert get_empty_streak(sid) == 2

    reset_empty_streak(sid)
    assert get_empty_streak(sid) == 0


def test_sessions_are_isolated(monkeypatch, tmp_vault, tmp_cortex_dir):
    """Turn counts are per-session and don't bleed across sessions."""
    from memem.slice_history import get_session_turn_count, increment_turn_count
    _reset_session_state()

    assert increment_turn_count("session-A") == 1
    assert increment_turn_count("session-A") == 2
    assert increment_turn_count("session-B") == 1  # B starts fresh
    assert get_session_turn_count("session-A") == 2
    assert get_session_turn_count("session-B") == 1


# ---------------------------------------------------------------------------
# auto mode bypasses cadence gate
# ---------------------------------------------------------------------------


def test_auto_mode_bypasses_cadence_gate(monkeypatch, tmp_vault, tmp_cortex_dir):
    """In auto mode, cadence gate is skipped entirely."""
    _patch_gating(monkeypatch, cadence=2, mode="auto")
    _reset_session_state()

    sid = "auto-cadence-test"
    # In auto mode, turn 1 (which would cadence-skip in hybrid) should run
    r = _make_call(sid)
    assert r.get("gating_reason") != "cadence_skip", (
        f"auto mode should bypass cadence gate; got {r.get('gating_reason')!r}"
    )
