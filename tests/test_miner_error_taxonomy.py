"""Tests for the TransientError/PermanentError taxonomy (miner_errors.py) and
the inverted classification chokepoint in _run_server_command (m8).

The key invariant being tested: an unknown exit code MUST classify as
PermanentError, never TransientError. Before this change, unknown codes
raised RetryableMinerError (= transient) which could trigger spawn-storms.
"""

from unittest.mock import MagicMock, patch

import pytest

from memem.miner_daemon import (
    FatalMinerError,
    RetryableMinerError,
    _run_server_command,
)
from memem.miner_errors import PermanentError, TransientError
from memem.miner_protocol import FATAL_EXIT_CODE, TRANSIENT_EXIT_CODE

# ---------------------------------------------------------------------------
# 1. Hierarchy tests
# ---------------------------------------------------------------------------


def test_transient_error_is_runtime_error():
    """TransientError must inherit from RuntimeError."""
    assert issubclass(TransientError, RuntimeError)
    exc = TransientError("something transient")
    assert isinstance(exc, RuntimeError)


def test_permanent_error_is_runtime_error():
    """PermanentError must inherit from RuntimeError."""
    assert issubclass(PermanentError, RuntimeError)
    exc = PermanentError("something permanent")
    assert isinstance(exc, RuntimeError)


# ---------------------------------------------------------------------------
# 2. Alias resolution tests
# ---------------------------------------------------------------------------


def test_aliases_resolve_to_taxonomy():
    """RetryableMinerError and FatalMinerError must be aliases for the new
    taxonomy classes — not independent classes — so backward-compat imports
    continue to work and isinstance checks match both names."""
    assert RetryableMinerError is TransientError, (
        "RetryableMinerError must be TransientError (alias)"
    )
    assert FatalMinerError is PermanentError, (
        "FatalMinerError must be PermanentError (alias)"
    )


# ---------------------------------------------------------------------------
# Helper: build a fake Popen context manager
# ---------------------------------------------------------------------------


def _make_fake_popen(returncode: int, stdout: str = "", stderr: str = ""):
    """Return a mock that behaves like subprocess.Popen used as a context manager.

    The real code does:
        with subprocess.Popen(...) as p:
            stdout, stderr = p.communicate(timeout=...)
        if p.returncode != 0: ...
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    proc.pid = 12345
    # Support context manager protocol
    proc.__enter__ = MagicMock(return_value=proc)
    proc.__exit__ = MagicMock(return_value=False)
    return proc


# ---------------------------------------------------------------------------
# 3. Classification chokepoint inversion tests
# ---------------------------------------------------------------------------


def test_unknown_exit_code_classified_as_permanent():
    """returncode=99 (unknown) must raise PermanentError, NOT TransientError.

    This is the regression test for the inversion: before this change,
    returncode=99 raised RetryableMinerError which could cause spawn-storms
    if the session's error message also matched no fatal pattern.
    """
    proc = _make_fake_popen(returncode=99, stderr="some unknown error")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=proc), pytest.raises(PermanentError):
        _run_server_command(["--mine-session", "/fake/session.jsonl"])


def test_unknown_exit_code_does_not_raise_transient():
    """Confirm the negative: returncode=99 must NOT raise TransientError."""
    proc = _make_fake_popen(returncode=99, stderr="some unknown error")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=proc):
        with pytest.raises(Exception) as exc_info:
            _run_server_command(["--mine-session", "/fake/session.jsonl"])
        assert not isinstance(exc_info.value, TransientError), (
            "Unknown exit code must not be classified as TransientError"
        )


def test_transient_exit_code_classified_as_transient():
    """returncode=TRANSIENT_EXIT_CODE must raise TransientError."""
    proc = _make_fake_popen(returncode=TRANSIENT_EXIT_CODE, stderr="transient failure")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=proc), pytest.raises(TransientError):
        _run_server_command(["--mine-session", "/fake/session.jsonl"])


def test_fatal_exit_code_classified_as_permanent():
    """returncode=FATAL_EXIT_CODE must raise PermanentError (unchanged behavior)."""
    proc = _make_fake_popen(returncode=FATAL_EXIT_CODE, stderr="fatal failure")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=proc), pytest.raises(PermanentError):
        _run_server_command(["--mine-session", "/fake/session.jsonl"])


def test_zero_exit_does_not_raise():
    """returncode=0 with expect_json=False must return stdout without raising."""
    proc = _make_fake_popen(returncode=0, stdout="ok", stderr="")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=proc):
        result = _run_server_command(["--rebuild-index"], expect_json=False)
    assert result == "ok"
