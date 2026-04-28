"""Tests for m5: persisting per-session failure counter to disk on each failure.

Covers:
1. attempts field persisted on first failure (STATUS_RETRYING)
2. attempts increments across multiple failures
3. attempts survives daemon restart (round-trip through _parse_state_line)
4. attempts defaults to 0 when not specified
5. STATUS_RETRYING is not terminal (session stays in queue)
6. STATUS_FAILED is still terminal (sanity check)
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
