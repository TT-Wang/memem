"""Tests for flock-based single-instance check and lockfile security."""

import os

import pytest

from memem import miner_daemon
from memem.miner_daemon import (
    _acquire_global_lock,
    _release_global_lock,
)


@pytest.fixture
def isolated_global_lock(tmp_cortex_dir, monkeypatch):
    """Redirect GLOBAL_LOCK_FILE into tmp dir for test isolation."""
    lock_path = tmp_cortex_dir / "miner.global.lock"
    monkeypatch.setattr(miner_daemon, "GLOBAL_LOCK_FILE", lock_path)
    yield lock_path
    # Teardown — release any held lock
    miner_daemon._release_global_lock()


def test_acquire_global_lock_returns_true_on_first_call(isolated_global_lock):
    """Fresh state: first acquire returns True."""
    result = _acquire_global_lock()
    assert result is True
    _release_global_lock()


def test_acquire_global_lock_returns_false_when_already_held(isolated_global_lock):
    """Second acquire without releasing returns False (lock already held)."""
    first = _acquire_global_lock()
    assert first is True
    second = _acquire_global_lock()
    assert second is False
    # Cleanup is handled by fixture teardown


def test_release_global_lock_clears_module_state(isolated_global_lock):
    """After release, _GLOBAL_LOCK_FH is None."""
    _acquire_global_lock()
    _release_global_lock()
    assert miner_daemon._GLOBAL_LOCK_FH is None


def test_release_after_release_is_noop(isolated_global_lock):
    """Calling release twice does not raise."""
    _acquire_global_lock()
    _release_global_lock()
    _release_global_lock()  # second call must be a no-op


def test_lockfile_created_with_mode_0600(isolated_global_lock):
    """Lockfile is created with mode 0600 (owner read+write only)."""
    lock_path = isolated_global_lock
    # Remove if it already exists so we test creation mode
    if lock_path.exists():
        lock_path.unlink()
    _acquire_global_lock()
    mode = os.stat(lock_path).st_mode & 0o777
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"
    _release_global_lock()


def test_lockfile_pid_written(isolated_global_lock):
    """After acquire, the lockfile contains the current process PID."""
    lock_path = isolated_global_lock
    _acquire_global_lock()
    content = lock_path.read_text().strip()
    assert content == str(os.getpid())
    _release_global_lock()


def test_dead_lock_can_be_reacquired(isolated_global_lock):
    """Acquire -> release -> re-acquire succeeds (no stale lock state)."""
    first = _acquire_global_lock()
    assert first is True
    _release_global_lock()
    second = _acquire_global_lock()
    assert second is True
    _release_global_lock()
