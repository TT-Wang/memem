"""Tests for memem/miner-wrapper.sh — exponential backoff and crash-rate guard.

Option B is used: the wrapper respects MEMEM_DAEMON_CMD env var so tests can
inject a fake daemon without modifying the script itself.
"""

import os
import pathlib
import subprocess
import textwrap

WORKTREE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER_PATH = os.path.join(WORKTREE_ROOT, "memem", "miner-wrapper.sh")


def _make_fake_daemon(tmp_path: pathlib.Path, exit_code: int) -> str:
    """Write a fake daemon shell script that exits with the given code.

    The wrapper calls ``$DAEMON_CMD run``, so the script must accept 'run'
    as its first argument (and silently succeed for anything else, e.g. 'stop').
    """
    script = tmp_path / "fake-daemon.sh"
    script.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "${{1:-}}" in
            run) exit {exit_code} ;;
            stop) exit 0 ;;
            status) exit 0 ;;
            *) exit 0 ;;
        esac
        """)
    )
    script.chmod(0o755)
    return str(script)


def _wrapper_env(tmp_path: pathlib.Path, fake_daemon: str, backoff_initial: int = 10) -> dict:
    """Build an environment dict that points the wrapper at the fake daemon.

    backoff_initial: override MEMEM_BACKOFF_INITIAL (default 10s).  Set to a
    small value in tests that need to observe multiple crash cycles quickly.
    """
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir(exist_ok=True)
    log_file = memem_dir / "miner.log"
    env = os.environ.copy()
    env["MEMEM_DAEMON_CMD"] = fake_daemon
    env["MEMEM_DIR"] = str(memem_dir)
    env["LOG_FILE"] = str(log_file)
    env["MEMEM_BACKOFF_INITIAL"] = str(backoff_initial)
    # Prevent the ephemeral-test-state guard from blocking the _loop command.
    env["MEMEM_ALLOW_TEST_MINER"] = "1"
    # Ensure no CORTEX_DIR leaks in
    env.pop("CORTEX_DIR", None)
    return env


# ---------------------------------------------------------------------------
# Test 1 — syntax check
# ---------------------------------------------------------------------------


def test_wrapper_syntax_valid():
    """bash -n must return exit 0 (script is syntactically valid)."""
    result = subprocess.run(
        ["bash", "-n", WRAPPER_PATH],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"bash -n failed:\n{result.stderr.decode()}"
    )


# ---------------------------------------------------------------------------
# Test 2 — 5-in-60s crash guard exits the loop
# ---------------------------------------------------------------------------


def test_wrapper_stops_after_5_crashes_in_60s(tmp_path):
    """Fake daemon that always exits with code 1 should trigger the crash guard.

    The guard fires after 5 crashes within 60 s. We use MEMEM_BACKOFF_INITIAL=1
    so that the backoff sequence is 1→2→4→8 s (total ~15 s for 5 crashes),
    well within the 60 s sliding window. The guard fires on crash #5 without
    sleeping, so the wrapper exits in under 30 s.
    """
    fake = _make_fake_daemon(tmp_path, exit_code=1)
    # Small initial backoff so 5 crashes accumulate within the 60 s window.
    # With backoff=1: crashes at ~t=0,1,3,7,15 — all within 60 s.
    env = _wrapper_env(tmp_path, fake, backoff_initial=1)
    log_file = env["LOG_FILE"]

    result = subprocess.run(
        ["bash", WRAPPER_PATH, "_loop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = result.stdout + result.stderr
    # Read the log file too (wrapper appends daemon stderr there but prints its
    # own messages to stdout)
    log_contents = ""
    if os.path.exists(log_file):
        with open(log_file) as f:
            log_contents = f.read()
    all_output = combined + log_contents

    assert "refusing to restart" in all_output, (
        f"Expected 'refusing to restart' in output.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\nlog:\n{log_contents}"
    )
    # Wrapper must have exited on its own (not killed by timeout)
    assert result.returncode is not None


# ---------------------------------------------------------------------------
# Test 3 — exit 75 causes immediate stop (regression)
# ---------------------------------------------------------------------------


def test_wrapper_does_not_restart_on_exit_75(tmp_path):
    """Daemon that exits 75 must cause the wrapper to log 'exited permanently'."""
    fake = _make_fake_daemon(tmp_path, exit_code=75)
    env = _wrapper_env(tmp_path, fake)

    result = subprocess.run(
        ["bash", WRAPPER_PATH, "_loop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert "exited permanently" in result.stdout, (
        f"Expected 'exited permanently'.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Test 4 — exit 0 causes immediate stop (regression)
# ---------------------------------------------------------------------------


def test_wrapper_does_not_restart_on_exit_0(tmp_path):
    """Daemon that exits 0 must cause the wrapper to log 'exited permanently'."""
    fake = _make_fake_daemon(tmp_path, exit_code=0)
    env = _wrapper_env(tmp_path, fake)

    result = subprocess.run(
        ["bash", WRAPPER_PATH, "_loop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert "exited permanently" in result.stdout, (
        f"Expected 'exited permanently'.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.returncode == 0
