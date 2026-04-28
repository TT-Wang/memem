"""Tests for heartbeat file and rich --status output (m16).

Covers:
- _write_heartbeat creates/updates HEARTBEAT_FILE with parsable timestamp
- _write_heartbeat handles missing MEMEM_DIR gracefully
- status_daemon shows heartbeat age, breaker state, log tail, PID info
"""

import importlib
import time

import pytest

from memem import miner_daemon


@pytest.fixture
def isolated_miner(tmp_cortex_dir, monkeypatch):
    """Redirect all miner_daemon module-level paths into tmp_cortex_dir.

    Reloads miner_daemon so MEMEM_DIR-derived constants (PID_FILE, LOG_FILE,
    HEARTBEAT_FILE) resolve to the isolated tmp directory.
    """
    monkeypatch.setenv("MEMEM_DIR", str(tmp_cortex_dir))
    importlib.reload(miner_daemon)
    yield tmp_cortex_dir
    # Restore to a clean state after each test
    importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# _write_heartbeat tests
# ---------------------------------------------------------------------------


def test_heartbeat_file_created_by_write_heartbeat(isolated_miner):
    """_write_heartbeat creates HEARTBEAT_FILE with a parsable integer timestamp."""
    before = int(time.time())
    miner_daemon._write_heartbeat()
    assert miner_daemon.HEARTBEAT_FILE.exists(), "HEARTBEAT_FILE should exist after _write_heartbeat"
    content = miner_daemon.HEARTBEAT_FILE.read_text().strip()
    ts = int(content)  # must not raise
    after = int(time.time())
    assert before <= ts <= after, f"Timestamp {ts} should be between {before} and {after}"


def test_heartbeat_handles_missing_dir(tmp_path, monkeypatch):
    """_write_heartbeat does not raise even when MEMEM_DIR does not exist yet."""
    nonexistent_dir = tmp_path / "no_such_dir"
    monkeypatch.setenv("MEMEM_DIR", str(nonexistent_dir))
    importlib.reload(miner_daemon)
    try:
        # Should not raise — it creates the directory or warns via log
        miner_daemon._write_heartbeat()
    except OSError:
        pytest.fail("_write_heartbeat raised OSError on missing MEMEM_DIR")
    finally:
        importlib.reload(miner_daemon)


# ---------------------------------------------------------------------------
# status_daemon output tests
# ---------------------------------------------------------------------------


def test_status_daemon_with_heartbeat(isolated_miner, capsys):
    """status_daemon shows 'Heartbeat:' with ISO timestamp when file exists."""
    miner_daemon._write_heartbeat()
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert "Heartbeat:" in out
    # Should include "s ago"
    assert "s ago" in out


def test_status_daemon_missing_heartbeat(isolated_miner, capsys):
    """status_daemon shows missing message when HEARTBEAT_FILE is absent."""
    # Ensure no heartbeat file exists
    if miner_daemon.HEARTBEAT_FILE.exists():
        miner_daemon.HEARTBEAT_FILE.unlink()
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert "Heartbeat: missing" in out


def test_status_daemon_includes_breaker_state(isolated_miner, capsys):
    """status_daemon output includes Circuit breaker section with state key."""
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert "Circuit breaker:" in out
    assert "state:" in out


def test_status_daemon_shows_log_tail(isolated_miner, capsys):
    """status_daemon shows the last 20 lines of the log file.

    We use zero-padded line numbers so that "log line 01" does not partially
    match "log line 10" (avoids spurious substring collisions in the assertion).
    """
    # Write 25 lines to the log file using zero-padded numbers
    lines = [f"log line {i:03d}" for i in range(1, 26)]
    miner_daemon.LOG_FILE.write_text("\n".join(lines) + "\n")
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    # The last 20 lines (006–025) should appear in the output
    for i in range(6, 26):
        assert f"log line {i:03d}" in out, f"Expected 'log line {i:03d}' in status output"
    # The first 5 lines should NOT appear (they were trimmed)
    for i in range(1, 6):
        assert f"log line {i:03d}" not in out, f"'log line {i:03d}' should not appear (trimmed)"


def test_status_daemon_handles_missing_log(isolated_miner, capsys):
    """status_daemon shows '(no log file)' when LOG_FILE is absent."""
    if miner_daemon.LOG_FILE.exists():
        miner_daemon.LOG_FILE.unlink()
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert "(no log file)" in out


def test_status_daemon_no_daemon_running(isolated_miner, capsys):
    """status_daemon shows 'not running' when there is no PID file."""
    if miner_daemon.PID_FILE.exists():
        miner_daemon.PID_FILE.unlink()
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert "not running" in out


def test_status_daemon_shows_running_when_pid_exists(isolated_miner, capsys, monkeypatch):
    """status_daemon shows 'running (PID ...)' when PID file contains current PID."""
    import os
    pid = os.getpid()
    miner_daemon.PID_FILE.write_text(str(pid))
    miner_daemon.status_daemon()
    out = capsys.readouterr().out
    assert f"running (PID {pid})" in out
