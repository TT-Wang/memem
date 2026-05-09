"""Regression tests for miner subprocess process-group isolation (m3, m4)."""

import signal
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from memem import miner_daemon
from memem.miner_daemon import (
    SUBPROCESS_KILL_GRACE_SECONDS,
    SUBPROCESS_TIMEOUT_SECONDS,
    RetryableMinerError,
    _is_fatal_api_error,
)


def _make_popen_mock(returncode=0, stdout="{}", stderr=""):
    """Helper to create a Popen context-manager mock with communicate returning given values."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = returncode
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.__enter__ = MagicMock(return_value=mock_proc)
    mock_proc.__exit__ = MagicMock(return_value=False)
    return mock_proc


# ---------------------------------------------------------------------------
# m3 tests — updated to mock subprocess.Popen (m4 replaced subprocess.run)
# ---------------------------------------------------------------------------

def test_run_server_command_uses_start_new_session():
    """All subprocess calls in the miner path must run in their own session
    so we can kill the whole process group on timeout/shutdown without
    leaking grandchildren (e.g., the LLM CLI's MCP child processes)."""
    mock_proc = _make_popen_mock(returncode=0, stdout="", stderr="")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc) as mock_popen:
        miner_daemon._run_server_command(["--noop"], expect_json=False)
    assert mock_popen.called
    _, kwargs = mock_popen.call_args
    assert kwargs.get("start_new_session") is True, (
        "subprocess.Popen must use start_new_session=True for process-group control"
    )


def test_run_server_command_json_path_uses_start_new_session():
    """start_new_session must be set even when expect_json=True (the default path)."""
    mock_proc = _make_popen_mock(returncode=0, stdout='{"key": "value"}', stderr="")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc) as mock_popen:
        result = miner_daemon._run_server_command(["--some-arg"], expect_json=True)
    assert mock_popen.called
    _, kwargs = mock_popen.call_args
    assert kwargs.get("start_new_session") is True, (
        "subprocess.Popen must use start_new_session=True regardless of expect_json"
    )
    assert result == {"key": "value"}


def test_run_server_command_propagates_env():
    """PYTHONPATH is injected into the subprocess environment — confirm env is passed."""
    mock_proc = _make_popen_mock(returncode=0, stdout="", stderr="")
    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc) as mock_popen:
        miner_daemon._run_server_command(["--noop"], expect_json=False)
    _, kwargs = mock_popen.call_args
    assert "env" in kwargs, "env dict must be forwarded to subprocess.Popen"
    assert "PYTHONPATH" in kwargs["env"], "PYTHONPATH must be set in the subprocess env"


# ---------------------------------------------------------------------------
# m4 tests — timeout, killpg, two-phase kill, ProcessLookupError swallowing
# ---------------------------------------------------------------------------

def test_subprocess_timeout_triggers_killpg_and_raises():
    """On TimeoutExpired, killpg(SIGTERM) is called and RetryableMinerError is raised."""
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.returncode = -15
    # First communicate raises TimeoutExpired; second (grace) succeeds
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="test", timeout=SUBPROCESS_TIMEOUT_SECONDS),
        ("", ""),
    ]
    mock_proc.__enter__ = MagicMock(return_value=mock_proc)
    mock_proc.__exit__ = MagicMock(return_value=False)

    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc), \
         patch("memem.miner_daemon.os.killpg") as mock_killpg, \
         patch("memem.miner_daemon.os.getpgid", return_value=99999), \
         pytest.raises(RetryableMinerError) as exc_info:
        miner_daemon._run_server_command(["--noop"], expect_json=False)

    # SIGTERM must have been sent to the process group
    mock_killpg.assert_any_call(99999, signal.SIGTERM)
    assert "timed out" in str(exc_info.value)


def test_subprocess_timeout_message_classified_as_transient_by_is_fatal_api_error():
    """v1.7 m1: subprocess timeouts are TRANSIENT (retryable), NOT fatal.
    The per-session timeout cap (MEMEM_MAX_SESSION_TIMEOUTS) handles repeated
    cases; killing the entire daemon on a single huge session was wrong.
    Matches the parametrize fix in test_miner_auth_crush.py."""
    timeout_exc = RetryableMinerError(
        f"subprocess timed out after {SUBPROCESS_TIMEOUT_SECONDS}s; killed process group"
    )
    assert _is_fatal_api_error(timeout_exc) is False, (
        "_is_fatal_api_error must return False for timeout errors (v1.7 m1 reclassification)"
    )


def test_subprocess_timeout_double_phase_kill():
    """When both communicate calls time out, killpg is called twice:
    first with SIGTERM, then with SIGKILL."""
    mock_proc = MagicMock()
    mock_proc.pid = 77777
    mock_proc.returncode = -9
    # First communicate: initial timeout
    # Second communicate (grace period): also times out
    # Third communicate (post-SIGKILL): succeeds
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="test", timeout=SUBPROCESS_TIMEOUT_SECONDS),
        subprocess.TimeoutExpired(cmd="test", timeout=SUBPROCESS_KILL_GRACE_SECONDS),
        ("", ""),
    ]
    mock_proc.__enter__ = MagicMock(return_value=mock_proc)
    mock_proc.__exit__ = MagicMock(return_value=False)

    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc), \
         patch("memem.miner_daemon.os.killpg") as mock_killpg, \
         patch("memem.miner_daemon.os.getpgid", return_value=77777), \
         pytest.raises(RetryableMinerError):
        miner_daemon._run_server_command(["--noop"], expect_json=False)

    # Must be called exactly twice: once with SIGTERM, once with SIGKILL
    assert mock_killpg.call_count == 2
    calls = mock_killpg.call_args_list
    assert calls[0] == call(77777, signal.SIGTERM)
    assert calls[1] == call(77777, signal.SIGKILL)


def test_subprocess_lookup_error_during_killpg_swallowed():
    """If the process died between getpgid and killpg (ProcessLookupError),
    the error is swallowed and the original RetryableMinerError still propagates."""
    mock_proc = MagicMock()
    mock_proc.pid = 55555
    mock_proc.returncode = -15
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="test", timeout=SUBPROCESS_TIMEOUT_SECONDS),
        ("", ""),
    ]
    mock_proc.__enter__ = MagicMock(return_value=mock_proc)
    mock_proc.__exit__ = MagicMock(return_value=False)

    with patch("memem.miner_daemon.subprocess.Popen", return_value=mock_proc), \
         patch("memem.miner_daemon.os.killpg", side_effect=ProcessLookupError), \
         patch("memem.miner_daemon.os.getpgid", return_value=55555), \
         pytest.raises(RetryableMinerError) as exc_info:
        # ProcessLookupError from killpg must not escape; only RetryableMinerError
        miner_daemon._run_server_command(["--noop"], expect_json=False)

    assert "timed out" in str(exc_info.value)
