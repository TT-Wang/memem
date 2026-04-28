"""Tests for subprocess semaphore serialization (m10)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from memem import miner_daemon  # noqa: E402

# ---------------------------------------------------------------------------
# Test 1 — semaphore type and constant
# ---------------------------------------------------------------------------

def test_semaphore_is_threading_semaphore_with_value_one():
    """_subprocess_semaphore must be a threading.Semaphore with MAX=1."""
    assert miner_daemon.MAX_CONCURRENT_SUBPROCESSES == 1
    # threading.Semaphore is a factory function that returns an internal type;
    # check via type name to avoid isinstance issues with the factory result.
    sem = miner_daemon._subprocess_semaphore
    sem_type_name = type(sem).__name__
    assert "Semaphore" in sem_type_name, (
        f"Expected a Semaphore type, got {sem_type_name}"
    )


# ---------------------------------------------------------------------------
# Test 2 — concurrent calls are serialized
# ---------------------------------------------------------------------------

def test_concurrent_calls_serialize():
    """Three concurrent callers must be serialized — max 1 active at once."""
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_popen_init(*args, **kwargs):
        fake_proc = MagicMock()
        fake_proc.__enter__ = lambda self: self
        fake_proc.__exit__ = lambda *a: None

        def slow_communicate(timeout=None):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with active_lock:
                active -= 1
            return ("ok", "")

        fake_proc.communicate = slow_communicate
        fake_proc.returncode = 0
        fake_proc.pid = 12345
        return fake_proc

    with patch("memem.miner_daemon.subprocess.Popen", side_effect=fake_popen_init):
        threads = [
            threading.Thread(
                target=lambda: miner_daemon._run_server_command(["--noop"], expect_json=False)
            )
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert max_active == 1, f"Expected serialized execution, saw {max_active} concurrent"


# ---------------------------------------------------------------------------
# Test 3 — semaphore released even when Popen raises
# ---------------------------------------------------------------------------

def test_semaphore_released_on_exception():
    """If Popen raises, the semaphore must still be released by the with block."""
    with patch("memem.miner_daemon.subprocess.Popen", side_effect=OSError("boom")), \
         pytest.raises(OSError):
        miner_daemon._run_server_command(["--noop"], expect_json=False)

    # Semaphore must be available immediately — not blocked
    acquired = miner_daemon._subprocess_semaphore.acquire(blocking=False)
    assert acquired is True, "Semaphore was not released after Popen raised OSError"
    miner_daemon._subprocess_semaphore.release()
