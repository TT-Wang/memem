"""Regression tests for v0.11.1 miner auth-crush fix (commit af2334e).

Background: when the `claude` CLI was logged out, every mine attempt failed
with a 401 / "Not logged in" error that was classified as RetryableMinerError.
Failed sessions never got persisted as terminal state, so the same backlog was
retried every 60s, spawning thousands of `claude -p` subprocesses per hour.
One affected host accumulated 43,538 retry events over ~13 days.

These tests pin the four mechanisms that prevent the regression:

1. _is_fatal_api_error classifies auth/login/quota errors as fatal so the
   daemon exits via SystemExit(FATAL_EXIT_CODE) and the wrapper stops it.
2. _is_fatal_api_error does NOT classify ordinary retryable errors as fatal.
3. session_is_terminal treats both COMPLETE and FAILED as terminal so a
   session marked FAILED is skipped on subsequent polls.
4. session_is_terminal re-includes a FAILED session if the JSONL changes —
   so new content gets a fresh attempt.
"""

import importlib
from pathlib import Path

import pytest

from memem.miner_daemon import _is_fatal_api_error
from memem.miner_protocol import (
    MINER_STATE_VERSION,
    STATUS_COMPLETE,
    STATUS_FAILED,
)


@pytest.mark.parametrize(
    "message",
    [
        "Authentication_error: invalid token",
        "401 invalid authentication credentials",
        "Not logged in. Please run /login.",
        "Please run /login to continue",
        "You have hit your limit for this month",
        "rate limit exceeded, retry later",
        "API quota reached",
        "Command '['claude', '-p']' timed out after 300 seconds",
        "subprocess timed out",
        "mining timed out after 600.5 seconds",
    ],
)
def test_fatal_api_error_classifies_known_fatal_messages(message):
    """Each known fatal pattern must be detected (case-insensitive)."""
    assert _is_fatal_api_error(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "connection reset by peer",
        "read timeout after 30s",
        "Internal server error 500",
        "subprocess exited with non-zero status",
        "json decode error at line 42",
        "",
    ],
)
def test_fatal_api_error_lets_retryable_errors_through(message):
    """Ordinary retryable failures must NOT be classified as fatal — otherwise
    the daemon would stop on every transient hiccup."""
    assert _is_fatal_api_error(RuntimeError(message)) is False


def _write_session_jsonl(state_dir, name="abc123", content="x") -> "Path":
    """Create a fake session JSONL the miner would consider real."""
    sessions_dir = state_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    path = sessions_dir / f"{name}.jsonl"
    path.write_text(content)
    return path


def test_session_is_terminal_skips_failed_sessions(tmp_cortex_dir):
    """A session persisted as STATUS_FAILED must be skipped — that's what
    stops the per-poll retry storm once the 3-strike counter trips."""
    from memem import session_state
    importlib.reload(session_state)

    jsonl = _write_session_jsonl(tmp_cortex_dir)
    state = session_state.update_session_state(
        jsonl, STATUS_FAILED, message="auth fail"
    )

    assert session_state.session_is_terminal(jsonl, state) is True


def test_session_is_terminal_skips_complete_sessions(tmp_cortex_dir):
    """The fix widened terminal-state from COMPLETE-only to {COMPLETE, FAILED};
    the COMPLETE path must still work."""
    from memem import session_state
    importlib.reload(session_state)

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="def456")
    state = session_state.update_session_state(jsonl, STATUS_COMPLETE)

    assert session_state.session_is_terminal(jsonl, state) is True


def test_session_is_terminal_reincludes_failed_session_on_content_change(
    tmp_cortex_dir,
):
    """If a FAILED session's JSONL grows or its mtime moves, the fingerprint
    no longer matches and the session re-enters the queue. Without this, a
    transiently-failed session that later got real content would never be
    mined."""
    from memem import session_state
    importlib.reload(session_state)

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="ghi789", content="x")
    state = session_state.update_session_state(jsonl, STATUS_FAILED)
    assert session_state.session_is_terminal(jsonl, state) is True

    jsonl.write_text("x" + "y" * 100)

    assert session_state.session_is_terminal(jsonl, state) is False


def test_session_is_terminal_returns_false_for_unknown_status(tmp_cortex_dir):
    """Anything other than COMPLETE/FAILED must NOT be skipped — otherwise an
    in-progress (or new STATUS_X) session could get stuck."""
    from memem import session_state
    importlib.reload(session_state)

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="jkl012")
    bogus_state = {
        "session_id": "jkl012",
        "status": "in_progress",
        "version": MINER_STATE_VERSION,
        "mtime_ns": 0,
        "size": 0,
    }

    assert session_state.session_is_terminal(jsonl, bogus_state) is False


def test_session_is_terminal_handles_missing_state(tmp_cortex_dir):
    """No prior state means the session is fresh — never terminal."""
    from memem import session_state

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="mno345")

    assert session_state.session_is_terminal(jsonl, None) is False
    assert session_state.session_is_terminal(jsonl, {}) is False
