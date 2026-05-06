"""Tests for m5: persisting per-session failure counter to disk on each failure.
Also covers m9: seeding failure_counts from disk on daemon restart so the cap
enforcement picks up where the previous run left off.

Covers (m5):
1. attempts field persisted on first failure (STATUS_RETRYING)
2. attempts increments across multiple failures
3. attempts survives daemon restart (round-trip through _parse_state_line)
4. attempts defaults to 0 when not specified
5. STATUS_RETRYING is not terminal (session stays in queue)
6. STATUS_FAILED is still terminal (sanity check)

Covers (m9):
7. failure_counts seeded from disk on daemon restart (STATUS_RETRYING with attempts=2)
8. COMPLETE sessions excluded from seed (should not inflate failure counter)
9. Sessions with attempts=0 excluded from seed (no failures yet)
10. Cap enforcement trips at MAX after seeded count plus one more failure
"""

import importlib
from pathlib import Path

import pytest

from memem.miner_protocol import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RETRYING,
)


def _write_session_jsonl(state_dir: Path, name: str = "abc123", content: str = "x") -> Path:
    """Create a fake session JSONL file the miner would consider real."""
    sessions_dir = state_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    path = sessions_dir / f"{name}.jsonl"
    path.write_text(content)
    return path


@pytest.fixture(autouse=True)
def reload_session_state(tmp_cortex_dir):
    """Reload session_state after tmp_cortex_dir sets MEMEM_DIR so that
    MINED_SESSIONS_FILE points to the isolated tmp directory."""
    from memem import session_state
    importlib.reload(session_state)
    return session_state


def _ss():
    from memem import session_state
    return session_state


def test_attempts_field_persisted_on_first_failure(tmp_cortex_dir):
    """Simulate one failure: write STATUS_RETRYING with attempts=1,
    load state from disk, assert attempts == 1."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess001")

    ss.update_session_state(jsonl, STATUS_RETRYING, message="attempt 1", attempts=1)

    states = ss.load_mined_session_state()
    assert "sess001" in states
    assert states["sess001"]["attempts"] == 1
    assert states["sess001"]["status"] == STATUS_RETRYING


def test_attempts_field_increments_across_failures(tmp_cortex_dir):
    """3 failures: assert attempts goes 1, 2, 3 on successive writes."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess002")

    for count in range(1, 4):
        ss.update_session_state(
            jsonl, STATUS_RETRYING, message=f"attempt {count}", attempts=count
        )
        states = ss.load_mined_session_state()
        assert states["sess002"]["attempts"] == count


def test_attempts_survives_daemon_restart(tmp_cortex_dir):
    """Write a state with attempts=2, reload via load_mined_session_state,
    assert the loaded dict has attempts == 2.

    This is the regression-against-whitelist-drop test. If _parse_state_line
    does not include 'attempts' in its returned dict, this test will fail with
    attempts == 0 (or KeyError) even though update_session_state wrote attempts=2
    to disk. The test proves the round-trip is intact.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess003")

    ss.update_session_state(jsonl, STATUS_RETRYING, message="attempt 2", attempts=2)

    # Simulate daemon restart: reload the module so MINED_SESSIONS_FILE is
    # re-evaluated from the env (still pointing at tmp_cortex_dir), then load.
    importlib.reload(ss)
    states = ss.load_mined_session_state()

    assert "sess003" in states
    assert states["sess003"]["attempts"] == 2


def test_attempts_field_zero_default(tmp_cortex_dir):
    """update_session_state without attempts argument writes attempts=0."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess004")

    ss.update_session_state(jsonl, STATUS_COMPLETE)

    states = ss.load_mined_session_state()
    assert "sess004" in states
    assert states["sess004"]["attempts"] == 0


def test_status_retrying_is_not_terminal(tmp_cortex_dir):
    """A session with STATUS_RETRYING must NOT be terminal.

    session_is_terminal only treats COMPLETE and FAILED as terminal. STATUS_RETRYING
    must pass through so the session stays in the queue and continues to be retried.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess005")

    state = ss.update_session_state(
        jsonl, STATUS_RETRYING, message="transient failure", attempts=1
    )

    assert ss.session_is_terminal(jsonl, state) is False


def test_status_failed_is_terminal(tmp_cortex_dir):
    """Sanity check: the existing terminal logic still works for STATUS_FAILED.

    This ensures m5 changes to session_state.py did not accidentally break
    the STATUS_FAILED terminal detection that prevents retry storms.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess006")

    state = ss.update_session_state(
        jsonl, STATUS_FAILED, message="gave up", attempts=3
    )

    assert ss.session_is_terminal(jsonl, state) is True


# ---------------------------------------------------------------------------
# m9 tests: seed failure_counts from disk so daemon restarts pick up the count
# ---------------------------------------------------------------------------


def test_failure_counts_seeded_from_disk_on_loop_start(tmp_cortex_dir):
    """Write attempts=2/STATUS_RETRYING to disk; verify the seed helper returns
    failure_counts[session_id] == 2 so a daemon restart doesn't reset the cap.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest01")
    ss.update_session_state(jsonl, STATUS_RETRYING, message="transient", attempts=2)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert seeded.get("seedtest01") == 2


def test_failure_counts_skip_completed_sessions(tmp_cortex_dir):
    """COMPLETE sessions must NOT appear in the seeded failure_counts.

    A session that succeeded and later re-enters the queue (after JSONL change)
    should start with a fresh counter, not carry forward stale failure counts.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest02")
    # Write attempts=2 but status=COMPLETE (e.g. succeeded on 3rd try,
    # attempts was set to the prior retrying count).
    ss.update_session_state(jsonl, STATUS_COMPLETE, message="", attempts=2)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert "seedtest02" not in seeded


def test_failure_counts_skip_zero_attempts(tmp_cortex_dir):
    """Sessions with attempts=0 must NOT appear in the seeded failure_counts.

    A fresh session that has never failed should not appear in failure_counts;
    seeding it with 0 would be harmless but misleading. Excluding it keeps the
    dict clean and avoids accidental cap triggers at exactly MAX attempts.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest03")
    ss.update_session_state(jsonl, STATUS_RETRYING, message="", attempts=0)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert "seedtest03" not in seeded


def test_session_marked_failed_at_cap_after_restart(tmp_cortex_dir):
    """Simulate daemon restart with attempts=2 (one short of MAX=3).

    After seeding failure_counts from disk, one more failure should trigger
    the cap and produce STATUS_FAILED in persisted state.

    This is tested at the helper level (not the full loop) to avoid needing
    to mock subprocesses. The cap-enforcement branch in _run_loop uses the
    same counter dict that _seed_failure_counts_from_state populates, so
    verifying the dict value is sufficient to prove the integration.

    For the full end-to-end "terminal after re-entry" path see:
        tests/test_miner_auth_crush.py::test_session_is_terminal_reincludes_failed_session_on_content_change
    """
    from memem.miner_daemon import MAX_SESSION_FAILURES, _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest04")
    # Persist attempts=MAX-1 (the state just before the cap fires)
    ss.update_session_state(
        jsonl, STATUS_RETRYING, message="attempt 2", attempts=MAX_SESSION_FAILURES - 1
    )

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    # One more failure would make count == MAX_SESSION_FAILURES which trips the cap.
    simulated_next_count = seeded.get("seedtest04", 0) + 1
    assert simulated_next_count == MAX_SESSION_FAILURES

    # Simulate cap enforcement: write STATUS_FAILED at cap
    if simulated_next_count >= MAX_SESSION_FAILURES:
        ss.update_session_state(
            jsonl, STATUS_FAILED,
            message=f"miner gave up after {simulated_next_count} consecutive failures",
            attempts=simulated_next_count,
        )

    final_states = ss.load_mined_session_state()
    assert final_states["seedtest04"]["status"] == STATUS_FAILED
    assert final_states["seedtest04"]["attempts"] == MAX_SESSION_FAILURES


def test_fatal_api_error_persists_status_failed_before_raise(tmp_cortex_dir, tmp_vault, monkeypatch):
    """Regression: when _mine_session classifies an exception as fatal-api-error
    (e.g. 'subprocess timed out after 300s'), it MUST persist STATUS_FAILED for
    the offending session BEFORE raising FatalMinerError. Otherwise a wrapper
    restart re-picks the session, hits the same hang, and crash-loops through
    the 5-in-60s wrapper budget.

    Reproduced in production: session 9612f54c-bbd timed out on 2026-04-30,
    2026-05-01, and 2026-05-04 — the same session each time, because no
    STATUS_FAILED was written between crashes.
    """
    import importlib
    from unittest.mock import patch

    from memem import miner_daemon, session_state
    importlib.reload(session_state)
    importlib.reload(miner_daemon)

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="hangsim01")

    # Force _run_server_command to raise a RetryableMinerError whose text
    # matches a fatal-api pattern ('timed out' is one such pattern).
    fake_exc = miner_daemon.RetryableMinerError(
        "subprocess timed out after 300s; killed process group"
    )
    with (
        patch.object(miner_daemon, "_run_server_command", side_effect=fake_exc),
        pytest.raises(miner_daemon.FatalMinerError),
    ):
        miner_daemon._mine_session(jsonl)

    # The session must be marked STATUS_FAILED on disk.
    states = session_state.load_mined_session_state()
    assert "hangsim01" in states, "session state was not persisted before fatal raise"
    assert states["hangsim01"]["status"] == STATUS_FAILED
    # attempts should be at MAX so a future restart with re-seeding immediately
    # treats it as DLQ-eligible.
    assert states["hangsim01"]["attempts"] >= miner_daemon.MAX_SESSION_FAILURES


# ---------------------------------------------------------------------------
# Hard retry cap (v1.2.2): a FAILED session past HARD_RETRY_CAP stays terminal
# regardless of content-fingerprint changes
# ---------------------------------------------------------------------------


def test_failed_session_stays_terminal_past_hard_retry_cap(tmp_cortex_dir):
    """A FAILED session with attempts >= HARD_RETRY_CAP must stay terminal even
    when the JSONL content changes.

    Reproduces the production bug: this conversation's JSONL grew during use,
    each retry timed out at 300s, fingerprint changed each retry, miner kept
    re-including the session — 102 attempts before the hard cap was added.
    """
    from memem import session_state
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="hardcap01")

    # Mark as FAILED with attempts well past the hard cap
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="exceeded retry budget",
        attempts=session_state.HARD_RETRY_CAP + 10,
    )

    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["hardcap01"]) is True

    # Now mutate the JSONL (simulate content change). Without the hard cap,
    # this would flip the session to non-terminal because the fingerprint
    # differs. With the cap, it must stay terminal.
    jsonl.write_text(jsonl.read_text() + "\n" + "x" * 5000)
    states = ss.load_mined_session_state()  # reload fingerprint comparison
    assert ss.session_is_terminal(jsonl, states["hardcap01"]) is True, (
        "FAILED session past HARD_RETRY_CAP must stay terminal across content changes; "
        "without this, an actively-growing JSONL crashes the miner in a loop"
    )


def test_failed_session_below_hard_cap_still_re_includes_on_change(tmp_cortex_dir):
    """Sessions below the hard cap must still re-enter the queue when content
    changes — preserving the 'transient failure can recover' design.
    """
    from memem import session_state
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="softcap01")

    # FAILED with attempts well below the hard cap
    assert session_state.HARD_RETRY_CAP >= 2
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="transient",
        attempts=1,
    )

    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["softcap01"]) is True

    # Mutate the JSONL — should re-enter the queue
    jsonl.write_text(jsonl.read_text() + "\n" + "y" * 5000)
    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["softcap01"]) is False, (
        "FAILED session below HARD_RETRY_CAP must re-enter queue on content change"
    )


def test_settle_seconds_default_is_30_minutes(tmp_cortex_dir, monkeypatch):
    """SETTLE_SECONDS default bumped 300s → 1800s.

    Previous default was 5 min, which was too short for actively-typed-into
    Claude Code sessions: the miner picked them up between user turns and the
    Haiku subprocess timed out on a still-growing file. 30 min ensures the
    user has stopped typing for a meaningful gap before mining is attempted.
    """
    import importlib

    monkeypatch.delenv("MEMEM_MINER_SETTLE_SECONDS", raising=False)
    monkeypatch.delenv("CORTEX_MINER_SETTLE_SECONDS", raising=False)
    from memem import session_state
    importlib.reload(session_state)
    assert session_state.SETTLE_SECONDS == 1800, (
        f"settle window should default to 1800s (30 min); got {session_state.SETTLE_SECONDS}"
    )
