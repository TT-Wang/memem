"""Tests for m6: SIGTERM/SIGINT graceful shutdown with flag-based drain.

Covers:
1. _request_shutdown sets the module-level _shutdown_requested flag
2. _run_loop returns immediately when _shutdown_requested is True
3. _run_loop calls _release_global_lock on shutdown
4. _run_loop removes PID_FILE on shutdown
"""

import signal
import threading
from unittest.mock import patch

import pytest

import memem.miner_daemon as miner_daemon
from memem.miner_daemon import _request_shutdown, _run_loop


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Ensure _shutdown_requested is False before and after every test."""
    miner_daemon._shutdown_requested = False
    yield
    miner_daemon._shutdown_requested = False


# --------------------------------------------------------------------------- #
# Test 1: _request_shutdown sets the flag                                      #
# --------------------------------------------------------------------------- #

def test_request_shutdown_sets_flag():
    assert miner_daemon._shutdown_requested is False
    _request_shutdown(signal.SIGTERM, None)
    assert miner_daemon._shutdown_requested is True


# --------------------------------------------------------------------------- #
# Test 2: _run_loop exits when shutdown flag is already set                    #
# --------------------------------------------------------------------------- #

def test_run_loop_exits_when_shutdown_flag_set(tmp_path, monkeypatch):
    # Point PID_FILE at a temp location so we don't create real files.
    fake_pid = tmp_path / "miner.pid"
    monkeypatch.setattr(miner_daemon, "PID_FILE", fake_pid)

    # _run_loop calls _ensure_installed_at() at the top — stub it out.
    with (
        patch("memem.miner_daemon._ensure_installed_at"),
        patch("memem.miner_daemon._release_global_lock"),
    ):
        miner_daemon._shutdown_requested = True
        result = []
        exc_holder = []

        def run():
            try:
                _run_loop()
                result.append("returned")
            except SystemExit:
                result.append("exited")
            except Exception as e:
                exc_holder.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=2)

    assert not t.is_alive(), "_run_loop did not return within 2 seconds"
    assert result == ["returned"], f"Unexpected result: {result}; exc: {exc_holder}"


# --------------------------------------------------------------------------- #
# Test 3: _run_loop releases global lock on shutdown                           #
# --------------------------------------------------------------------------- #

def test_run_loop_releases_flock_on_shutdown(tmp_path, monkeypatch):
    fake_pid = tmp_path / "miner.pid"
    monkeypatch.setattr(miner_daemon, "PID_FILE", fake_pid)

    with (
        patch("memem.miner_daemon._ensure_installed_at"),
        patch("memem.miner_daemon._release_global_lock") as mock_release,
    ):
        miner_daemon._shutdown_requested = True
        _run_loop()
        mock_release.assert_called_once()


# --------------------------------------------------------------------------- #
# Test 4: _run_loop removes PID_FILE on shutdown                               #
# --------------------------------------------------------------------------- #

def test_run_loop_removes_pid_file_on_shutdown(tmp_path, monkeypatch):
    fake_pid = tmp_path / "miner.pid"
    fake_pid.write_text("12345")  # simulate an existing PID file
    monkeypatch.setattr(miner_daemon, "PID_FILE", fake_pid)

    with (
        patch("memem.miner_daemon._ensure_installed_at"),
        patch("memem.miner_daemon._release_global_lock"),
    ):
        miner_daemon._shutdown_requested = True
        _run_loop()

    assert not fake_pid.exists(), "PID file was not removed on shutdown"
