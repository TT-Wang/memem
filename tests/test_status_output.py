"""Tests for memem/status.py render_status() function (m6).

Covers:
- render_status() returns a non-empty string
- Heartbeat band logic: OK / WARN / FAIL
- Missing heartbeat file doesn't crash, shows "missing" text
- Dead lock owner shows STALE
- Alive lock owner does not show STALE
- All 5 section headers are present
- Missing miner.log doesn't crash
"""

import importlib
import os
import time

import pytest


@pytest.fixture
def isolated_status(tmp_path, monkeypatch):
    """Redirect MEMEM_DIR and GLOBAL_LOCK_FILE into tmp_path for isolated testing.

    Reloads both miner_daemon and status modules so their module-level constants
    resolve to the isolated tmp directory.
    """
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    from memem import miner_daemon, models, status
    importlib.reload(models)
    importlib.reload(miner_daemon)
    importlib.reload(status)

    yield state

    # Restore clean state
    importlib.reload(models)
    importlib.reload(miner_daemon)
    importlib.reload(status)


def _get_render_status():
    """Return the render_status function from the (possibly reloaded) status module."""
    from memem import status as _status_mod
    return _status_mod.render_status


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_render_status_returns_string(isolated_status):
    """render_status() returns a non-empty string without raising."""
    render = _get_render_status()
    result = render()
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Heartbeat band tests
# ---------------------------------------------------------------------------


def _set_heartbeat_age(state_dir, age_seconds: int):
    """Create/touch HEARTBEAT_FILE with an mtime 'age_seconds' ago."""
    from memem import miner_daemon
    hb = miner_daemon.HEARTBEAT_FILE
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(str(int(time.time() - age_seconds)))
    target_mtime = time.time() - age_seconds
    os.utime(hb, (target_mtime, target_mtime))


def test_heartbeat_ok_band(isolated_status):
    """Heartbeat age 30s shows OK (not WARN or FAIL)."""
    _set_heartbeat_age(isolated_status, 30)
    render = _get_render_status()
    out = render()
    # Section [2] must contain OK band
    assert "✓ OK" in out
    # Should not have WARN or FAIL in the last-write line specifically
    lines = out.splitlines()
    last_write_lines = [ln for ln in lines if "Last write:" in ln]
    assert last_write_lines, "Expected 'Last write:' line in output"
    assert "✓ OK" in last_write_lines[0]
    assert "WARN" not in last_write_lines[0]
    assert "FAIL" not in last_write_lines[0]


def test_heartbeat_warn_band(isolated_status):
    """Heartbeat age 200s shows WARN."""
    _set_heartbeat_age(isolated_status, 200)
    render = _get_render_status()
    out = render()
    lines = out.splitlines()
    last_write_lines = [ln for ln in lines if "Last write:" in ln]
    assert last_write_lines, "Expected 'Last write:' line in output"
    assert "WARN" in last_write_lines[0]


def test_heartbeat_fail_band(isolated_status):
    """Heartbeat age 400s shows FAIL."""
    _set_heartbeat_age(isolated_status, 400)
    render = _get_render_status()
    out = render()
    lines = out.splitlines()
    last_write_lines = [ln for ln in lines if "Last write:" in ln]
    assert last_write_lines, "Expected 'Last write:' line in output"
    assert "FAIL" in last_write_lines[0]


def test_missing_heartbeat_file(isolated_status):
    """When heartbeat file is absent, section renders with 'missing' or 'no heartbeat' text."""
    from memem import miner_daemon
    if miner_daemon.HEARTBEAT_FILE.exists():
        miner_daemon.HEARTBEAT_FILE.unlink()

    render = _get_render_status()
    # Should not raise
    out = render()
    assert isinstance(out, str)
    # Should indicate file is absent — check either the legacy line or new section
    out_lower = out.lower()
    assert "missing" in out_lower or "no heartbeat" in out_lower


# ---------------------------------------------------------------------------
# Lock file tests
# ---------------------------------------------------------------------------


def test_dead_lock_owner_shown_stale(isolated_status, tmp_path):
    """A lock file containing a dead PID is shown as STALE."""
    from memem import miner_daemon

    dead_pid = 999999  # extremely unlikely to be a real process
    lock_file = miner_daemon.GLOBAL_LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(dead_pid))

    render = _get_render_status()
    out = render()
    assert "STALE" in out


def test_alive_lock_owner_shown_ok(isolated_status):
    """A lock file containing our own PID is shown as alive (not STALE)."""
    from memem import miner_daemon

    my_pid = os.getpid()
    lock_file = miner_daemon.GLOBAL_LOCK_FILE
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(my_pid))

    render = _get_render_status()
    out = render()
    assert "STALE" not in out


# ---------------------------------------------------------------------------
# All 5 sections present
# ---------------------------------------------------------------------------


def test_all_5_sections_present(isolated_status):
    """All 5 section headers must appear in the output."""
    render = _get_render_status()
    out = render()
    assert "Process tree" in out, "Section [1] Process tree missing"
    assert "Heartbeat" in out, "Section [2] Heartbeat missing"
    assert "Lock file" in out, "Section [3] Lock file missing"
    assert "Recent mining activity" in out, "Section [4] Recent mining activity missing"
    assert "Recursion-orphan counter" in out, "Section [5] Recursion-orphan counter missing"


# ---------------------------------------------------------------------------
# Missing log file
# ---------------------------------------------------------------------------


def test_no_log_file_no_crash(isolated_status):
    """With miner.log absent, section [5] still renders without raising."""
    from memem import miner_daemon

    # Ensure log file doesn't exist
    if miner_daemon.LOG_FILE.exists():
        miner_daemon.LOG_FILE.unlink()

    render = _get_render_status()
    # Must not raise
    out = render()
    assert "Recursion-orphan counter" in out
    # Should indicate no data
    assert "no data" in out.lower() or "no log file" in out.lower()
