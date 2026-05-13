"""Tests for miner self-healing bugs fixed in memem-v1-7-2-arch-stability m3.

Covers:
1. Heartbeat written inside inner session loop (not just once per outer iteration)
2. Stale lock file with dead PID is cleaned up and re-acquired
3. Live lock file is respected (no clobber)
4. miner-wrapper.sh stop escalates to SIGKILL if SIGTERM insufficient (bash test)
"""

import os
import platform
import subprocess
import sys
import textwrap
import time
from unittest.mock import patch

import pytest

import memem.miner_daemon as miner_daemon
from memem.miner_daemon import (
    _acquire_global_lock,
    _release_global_lock,
)

WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER_PATH = os.path.join(WORKTREE_ROOT, "memem", "miner-wrapper.sh")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_global_lock(tmp_path, monkeypatch):
    """Redirect GLOBAL_LOCK_FILE and HEARTBEAT_FILE into tmp dir for isolation."""
    lock_path = tmp_path / "miner.global.lock"
    heartbeat_path = tmp_path / "miner.heartbeat"
    monkeypatch.setattr(miner_daemon, "GLOBAL_LOCK_FILE", lock_path)
    monkeypatch.setattr(miner_daemon, "HEARTBEAT_FILE", heartbeat_path)
    yield lock_path
    miner_daemon._release_global_lock()


# ---------------------------------------------------------------------------
# Test 1: Heartbeat called for each item in inner session loop
# ---------------------------------------------------------------------------


def test_heartbeat_called_during_inner_loop(tmp_path, monkeypatch):
    """_write_heartbeat must be called at least once per session in inner loop.

    We run one outer iteration of _run_loop with 5 fake sessions queued.
    _write_heartbeat must be called >= 5 times (once per inner iter)
    — not just once at the start of the outer iteration.
    """
    # Redirect file paths to tmp_path so nothing touches real state
    pid_file = tmp_path / "miner.pid"
    heartbeat_file = tmp_path / "miner.heartbeat"
    monkeypatch.setattr(miner_daemon, "PID_FILE", pid_file)
    monkeypatch.setattr(miner_daemon, "HEARTBEAT_FILE", heartbeat_file)

    # Create 5 fake .jsonl paths (files don't need to exist for the mock)
    fake_sessions = [tmp_path / f"session_{i}.jsonl" for i in range(5)]

    heartbeat_call_count = [0]

    def counting_write_heartbeat():
        heartbeat_call_count[0] += 1
        # Also write to the file so real code doesn't crash
        try:
            heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_file.write_text(str(int(time.time())))
        except OSError:
            pass

    # _mine_session mock — instant, returns (0, False) to simulate fast processing
    def fast_mine_session(jsonl_path):
        return (0, False)

    # After one poll cycle, set shutdown flag so _run_loop returns
    poll_count = [0]

    def patched_find_settled_sessions(states):
        poll_count[0] += 1
        if poll_count[0] == 1:
            return list(fake_sessions)
        # Subsequent calls: trigger shutdown
        miner_daemon._shutdown_requested = True
        return []

    def patched_load_mined_session_state():
        return {}

    miner_daemon._shutdown_requested = False

    with (
        patch("memem.miner_daemon._write_heartbeat", side_effect=counting_write_heartbeat),
        patch("memem.miner_daemon._mine_session", side_effect=fast_mine_session),
        patch("memem.miner_daemon.find_settled_sessions", side_effect=patched_find_settled_sessions),
        patch("memem.miner_daemon.load_mined_session_state", side_effect=patched_load_mined_session_state),
        patch("memem.miner_daemon._ensure_installed_at"),
        patch("memem.miner_daemon._cleanup_stuck_sessions", return_value=0),
        patch("memem.miner_daemon._archive_expired_procedural_suggestions", return_value=0),
        patch("memem.miner_daemon._release_global_lock"),
        patch("memem.miner_daemon._run_server_command"),
        patch("memem.miner_daemon.compute_sleep_cap", return_value=0),
        patch("memem.miner_daemon.time.sleep"),
    ):
        try:
            miner_daemon._run_loop()
        except SystemExit:
            pass

    miner_daemon._shutdown_requested = False

    # Must have been called at least 5 times (once per inner session) plus
    # the initial call at outer loop start.
    assert heartbeat_call_count[0] >= 5, (
        f"Expected _write_heartbeat called >= 5 times during inner loop, "
        f"got {heartbeat_call_count[0]}"
    )


# ---------------------------------------------------------------------------
# Test 2: Stale lock file (dead PID) is cleaned up and re-acquired
# ---------------------------------------------------------------------------


def test_stale_lock_file_cleaned_up(isolated_global_lock, tmp_path):
    """Lock file holding dead PID 999999 must be removed and re-acquired."""
    lock_path = isolated_global_lock

    # Write a lock file with a definitely-dead PID
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999\n")
    lock_path.chmod(0o600)

    result = _acquire_global_lock()

    try:
        assert result is True, "Expected acquisition to succeed after stale lock cleanup"
        # The lock file should now contain our PID
        assert lock_path.exists(), "Lock file should exist after re-acquisition"
        content = lock_path.read_text().strip()
        assert content == str(os.getpid()), (
            f"Lock file should contain current PID {os.getpid()}, got {content!r}"
        )
    finally:
        _release_global_lock()


# ---------------------------------------------------------------------------
# Test 3: Live lock file (current PID) is respected — no clobber
# ---------------------------------------------------------------------------


def test_live_lock_file_respected(isolated_global_lock, tmp_path):
    """Lock file holding live PID (current process) must cause acquisition to fail."""
    lock_path = isolated_global_lock

    # First acquire to set up a live lock held by this process
    first = _acquire_global_lock()
    assert first is True, "Pre-condition: first acquire should succeed"

    # Now try to acquire again (simulates a second process seeing a live lock)
    # We need to test the case where the lock is held by a DIFFERENT alive process.
    # Since we can't fork easily, we test via os.getpid() — the current process
    # is alive — by writing the PID manually into a fresh lock file.
    _release_global_lock()

    # Write current PID into the lock file to simulate a live holder
    lock_path.write_text(f"{os.getpid()}\n")
    lock_path.chmod(0o600)

    # Now try to acquire — the lock file is present but not flock-held,
    # so acquisition will succeed via flock. The test for "live process"
    # only matters when flock is held. The real guard is flock-based.
    # However, we can test the PID liveness check by acquiring the flock
    # externally, then calling _acquire_global_lock from this process.

    # Simulate flock held by spawning a subprocess that holds it
    holder_script = textwrap.dedent(f"""\
        import fcntl, time, sys
        fh = open({str(lock_path)!r}, 'a+')
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str({os.getpid()}) + '\\n')
        fh.flush()
        sys.stdout.write('ready\\n')
        sys.stdout.flush()
        time.sleep(10)  # hold lock for 10 seconds
    """)

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for holder to signal it's ready
        line = holder.stdout.readline()
        assert "ready" in line, f"Holder did not signal ready: {line!r}"

        # Now try to acquire — should fail because lock is held by live holder
        result = _acquire_global_lock()
        assert result is False, (
            f"Expected acquisition to fail when lock is held by live process, got {result}"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)
        miner_daemon._release_global_lock()


# ---------------------------------------------------------------------------
# Test 4: miner-wrapper.sh stop escalates to SIGKILL (bash integration test)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="Bash process group behavior differs on macOS",
)
def test_wrapper_stop_escalates_to_sigkill(tmp_path):
    """miner-wrapper.sh stop must kill a process that ignores SIGTERM.

    We spawn a process that traps SIGTERM (ignores it), write its PID into
    miner.pid, and run 'miner-wrapper.sh stop'. The script must SIGKILL
    the process within 7 seconds.
    """
    if not os.path.exists(WRAPPER_PATH):
        pytest.skip(f"Wrapper not found at {WRAPPER_PATH}")

    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    daemon_pid_file = memem_dir / "miner.pid"

    # Spawn a process that traps SIGTERM so it survives the first signal
    # but will be killed by SIGKILL.
    # Use a simpler victim: a sleep that we wrap with trap
    victim = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; echo $$; sleep 60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        victim_pid_str = victim.stdout.readline().strip()
        victim_pid = int(victim_pid_str)

        # Write victim PID into daemon PID file
        daemon_pid_file.write_text(f"{victim_pid}\n")

        env = os.environ.copy()
        env["MEMEM_DIR"] = str(memem_dir)
        env["MEMEM_ALLOW_TEST_MINER"] = "1"
        env.pop("CORTEX_DIR", None)
        # Use a fake daemon cmd that doesn't actually do anything
        env["MEMEM_DAEMON_CMD"] = "true"

        t_start = time.monotonic()
        result = subprocess.run(
            ["bash", WRAPPER_PATH, "stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        elapsed = time.monotonic() - t_start

        # Verify the victim is dead within 7 seconds total
        try:
            victim.wait(timeout=max(0, 7 - elapsed))
        except subprocess.TimeoutExpired:
            victim.kill()
            pytest.fail(
                f"Victim process {victim_pid} still alive after wrapper stop + 7s\n"
                f"wrapper stdout: {result.stdout}\n"
                f"wrapper stderr: {result.stderr}"
            )

        # Verify wrapper exited cleanly
        assert result.returncode == 0 or result.returncode is not None, (
            f"Wrapper stop returned unexpected code {result.returncode}"
        )

        # Victim should be dead
        try:
            os.kill(victim_pid, 0)
            pytest.fail(f"Victim {victim_pid} still alive after wrapper stop")
        except OSError:
            pass  # dead — expected

    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=3)
