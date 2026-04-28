"""Regression tests for miner subprocess process-group isolation (m3, m4)."""

from unittest.mock import patch, MagicMock

import pytest

from memem import miner_daemon


def test_run_server_command_uses_start_new_session():
    """All subprocess calls in the miner path must run in their own session
    so we can kill the whole process group on timeout/shutdown without
    leaking grandchildren (e.g., the LLM CLI's MCP child processes)."""
    fake_result = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch("memem.miner_daemon.subprocess.run", return_value=fake_result) as mock_run:
        miner_daemon._run_server_command(["--noop"], expect_json=False)
    assert mock_run.called
    _, kwargs = mock_run.call_args
    assert kwargs.get("start_new_session") is True, (
        "subprocess.run must use start_new_session=True for process-group control"
    )


def test_run_server_command_json_path_uses_start_new_session():
    """start_new_session must be set even when expect_json=True (the default path)."""
    fake_result = MagicMock(returncode=0, stdout='{"key": "value"}', stderr="")
    with patch("memem.miner_daemon.subprocess.run", return_value=fake_result) as mock_run:
        result = miner_daemon._run_server_command(["--some-arg"], expect_json=True)
    assert mock_run.called
    _, kwargs = mock_run.call_args
    assert kwargs.get("start_new_session") is True, (
        "subprocess.run must use start_new_session=True regardless of expect_json"
    )
    assert result == {"key": "value"}


def test_run_server_command_propagates_env():
    """PYTHONPATH is injected into the subprocess environment — confirm env is passed."""
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("memem.miner_daemon.subprocess.run", return_value=fake_result) as mock_run:
        miner_daemon._run_server_command(["--noop"], expect_json=False)
    _, kwargs = mock_run.call_args
    assert "env" in kwargs, "env dict must be forwarded to subprocess.run"
    assert "PYTHONPATH" in kwargs["env"], "PYTHONPATH must be set in the subprocess env"
