"""Tests for memem.reaper — defensive orphan-process reaper (m5)."""

import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from memem import reaper as reaper_mod
from memem.reaper import reap_orphan_haiku_procs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc_info(ppid: int = 1, cmdline: str = "claude -p --model haiku", age: float = 300.0) -> dict:
    return {"ppid": ppid, "cmdline": cmdline, "age": age}


# ---------------------------------------------------------------------------
# Basic sweep tests (mocked /proc)
# ---------------------------------------------------------------------------

def test_no_orphans_returns_zero():
    """When there are no PIDs to inspect, reap_orphan_haiku_procs() returns 0."""
    with patch.object(reaper_mod, "_iter_pids", return_value=iter([])), \
         patch("pathlib.Path.exists", return_value=True):
        result = reap_orphan_haiku_procs(min_age_seconds=120)
    assert result == 0


def test_kills_orphan_haiku():
    """A matching orphan (ppid=1, claude -p --model haiku, age>min) is SIGKILL'd and counted."""
    fake_pid = 12345

    def fake_iter_pids():
        yield fake_pid

    fake_info = _make_proc_info(ppid=1, cmdline="claude -p --model haiku", age=300.0)

    with patch.object(reaper_mod, "_iter_pids", side_effect=fake_iter_pids), \
         patch.object(reaper_mod, "_read_proc_info", return_value=fake_info), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("os.kill") as mock_kill:
        result = reap_orphan_haiku_procs(min_age_seconds=120)

    assert result == 1
    mock_kill.assert_called_once_with(fake_pid, signal.SIGKILL)


def test_skips_young_orphan():
    """An orphan that is too young (age < min_age) must NOT be killed."""
    fake_pid = 22222

    def fake_iter_pids():
        yield fake_pid

    # age=30 < min_age_seconds=120
    fake_info = _make_proc_info(ppid=1, cmdline="claude -p --model haiku", age=30.0)

    with patch.object(reaper_mod, "_iter_pids", side_effect=fake_iter_pids), \
         patch.object(reaper_mod, "_read_proc_info", return_value=fake_info), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("os.kill") as mock_kill:
        result = reap_orphan_haiku_procs(min_age_seconds=120)

    assert result == 0
    mock_kill.assert_not_called()


def test_skips_non_orphan_haiku():
    """A haiku proc whose ppid != 1 (not an orphan) must NOT be killed."""
    fake_pid = 33333

    def fake_iter_pids():
        yield fake_pid

    # ppid=1234 (not init)
    fake_info = _make_proc_info(ppid=1234, cmdline="claude -p --model haiku", age=300.0)

    with patch.object(reaper_mod, "_iter_pids", side_effect=fake_iter_pids), \
         patch.object(reaper_mod, "_read_proc_info", return_value=fake_info), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("os.kill") as mock_kill:
        result = reap_orphan_haiku_procs(min_age_seconds=120)

    assert result == 0
    mock_kill.assert_not_called()


def test_skips_non_haiku_orphan():
    """An orphaned process that is NOT a haiku proc must NOT be killed."""
    fake_pid = 44444

    def fake_iter_pids():
        yield fake_pid

    # ppid=1 and old, but not claude at all
    fake_info = _make_proc_info(ppid=1, cmdline="python somethingelse", age=300.0)

    with patch.object(reaper_mod, "_iter_pids", side_effect=fake_iter_pids), \
         patch.object(reaper_mod, "_read_proc_info", return_value=fake_info), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("os.kill") as mock_kill:
        result = reap_orphan_haiku_procs(min_age_seconds=120)

    assert result == 0
    mock_kill.assert_not_called()


def test_handles_race_condition():
    """If os.kill raises ProcessLookupError (process died between detect+kill),
    the sweep continues without error and the count is 0 (didn't actually kill)."""
    fake_pid = 55555

    def fake_iter_pids():
        yield fake_pid

    fake_info = _make_proc_info(ppid=1, cmdline="claude -p --model haiku", age=300.0)

    with patch.object(reaper_mod, "_iter_pids", side_effect=fake_iter_pids), \
         patch.object(reaper_mod, "_read_proc_info", return_value=fake_info), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("os.kill", side_effect=ProcessLookupError):
        # Must not raise; count must be 0 (didn't kill)
        result = reap_orphan_haiku_procs(min_age_seconds=120)

    assert result == 0


# ---------------------------------------------------------------------------
# Smoke test — real /proc (Linux only)
# ---------------------------------------------------------------------------

def test_real_proc_smoke():
    """Smoke test: call with a very large min_age to ensure no real orphans are
    killed and the function returns a non-negative integer without crashing.
    Skipped on non-Linux."""
    if not Path("/proc").exists():
        pytest.skip("/proc not available — non-Linux host")

    result = reap_orphan_haiku_procs(min_age_seconds=99999)
    assert isinstance(result, int)
    assert result >= 0


# ---------------------------------------------------------------------------
# Miner integration smoke test
# ---------------------------------------------------------------------------

def test_miner_imports_reaper():
    """reap_orphan_haiku_procs must be importable from miner_daemon namespace."""
    from memem.miner_daemon import reap_orphan_haiku_procs as _fn  # noqa: PLC0415
    assert callable(_fn)
