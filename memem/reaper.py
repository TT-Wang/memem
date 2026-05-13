"""Defensive orphan-process reaper.

Even with the anti-recursion guard (v1.7.2 m12) preventing memem hooks from
firing inside memem-spawned headless `claude -p` calls, defense-in-depth:
the miner periodically reaps any leaked claude haiku procs that have
been adopted by init.
"""

import os
import signal
from collections.abc import Iterator
from pathlib import Path

import structlog

log = structlog.get_logger("memem-miner")


def _iter_pids() -> Iterator[int]:
    """Yield integer PIDs from /proc (Linux only)."""
    try:
        proc = Path("/proc")
        for entry in proc.iterdir():
            if entry.name.isdigit():
                yield int(entry.name)
    except (FileNotFoundError, PermissionError):
        return


def _read_proc_info(pid: int) -> dict | None:
    """Read ppid, cmdline, and wall age for *pid*.

    Returns a dict with keys ``ppid`` (int), ``cmdline`` (str, space-joined),
    and ``age`` (float, seconds since process start), or None if the process
    has disappeared or /proc is unavailable.

    Age is computed as: uptime_seconds - process_starttime_in_clock_ticks / HZ.
    /proc/uptime gives the system uptime in seconds.
    /proc/<pid>/stat field 22 (0-indexed: index 21) is the start time in
    clock ticks since boot.
    """
    try:
        stat_path = Path(f"/proc/{pid}/stat")
        stat_text = stat_path.read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return None

    try:
        # stat format: pid (comm) state ppid ... starttime ...
        # comm may contain spaces and parens; find the last ')' to split safely.
        rpar_idx = stat_text.rfind(")")
        after_comm = stat_text[rpar_idx + 2:]  # skip ') '
        fields = after_comm.split()
        # After comm+state: fields[0]=state, fields[1]=ppid, ...
        # starttime is field 22 overall (index 19 after comm removal, 0-based)
        # fields here: [state, ppid, pgrp, session, tty_nr, tpgid, flags,
        #               minflt, cminflt, majflt, cmajflt, utime, stime,
        #               cutime, cstime, priority, nice, num_threads, itrealvalue,
        #               starttime, ...]
        ppid = int(fields[1])
        starttime_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None

    try:
        cmdline_bytes = Path(f"/proc/{pid}/cmdline").read_bytes()
        # cmdline is NUL-separated bytes; decode each part then join with spaces
        parts_b = cmdline_bytes.rstrip(b"\x00").split(b"\x00")
        cmdline = " ".join(p.decode("utf-8", errors="replace") for p in parts_b).strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None

    try:
        uptime_text = Path("/proc/uptime").read_text()
        uptime_seconds = float(uptime_text.split()[0])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None

    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    start_seconds = starttime_ticks / hz
    age = uptime_seconds - start_seconds

    return {"ppid": ppid, "cmdline": cmdline, "age": age}


def reap_orphan_haiku_procs(min_age_seconds: int = 120) -> int:
    """Find and SIGKILL `claude -p ... --model haiku` procs whose parent is
    PID 1 (orphaned) and whose elapsed wall time exceeds min_age_seconds.

    Returns the count of procs killed. Logs each kill via structlog event
    'orphan_reaped' with pid + age. Logs the total via 'orphan_reaper_sweep'
    with reaped_count + min_age_seconds.

    Implementation: enumerate /proc, read /proc/<pid>/stat for ppid + starttime,
    read /proc/<pid>/cmdline for the command. Linux-only (skips silently with
    log.debug if /proc not present so tests still pass on macOS).
    """
    if not Path("/proc").exists():
        log.debug("orphan_reaper_skipped", reason="/proc not available (non-Linux)")
        return 0

    reaped = 0

    for pid in _iter_pids():
        info = _read_proc_info(pid)
        if info is None:
            continue

        # Must be adopted by init (pid 1)
        if info["ppid"] != 1:
            continue

        # Must be old enough
        if info["age"] < min_age_seconds:
            continue

        # cmdline must look like a `claude -p ... --model haiku` invocation
        cmdline = info["cmdline"]
        parts = cmdline.split()
        if not parts:
            continue

        # First token should be the claude binary path (ends with 'claude')
        binary = parts[0]
        if not (binary == "claude" or binary.endswith("/claude")):
            continue

        if "-p" not in parts:
            continue

        if "--model" not in parts:
            continue

        # Check haiku model substring anywhere in the joined cmdline
        if "haiku" not in cmdline.lower():
            continue

        # This process matches — SIGKILL it
        try:
            os.kill(pid, signal.SIGKILL)
            log.info("orphan_reaped", pid=pid, age=round(info["age"], 1))
            reaped += 1
        except ProcessLookupError:
            # Race: process died between detect and kill — treat as already gone
            log.debug("orphan_already_gone", pid=pid)
        except OSError as exc:
            log.warning("orphan_kill_failed", pid=pid, error=str(exc))

    log.debug("orphan_reaper_sweep", reaped_count=reaped, min_age_seconds=min_age_seconds)
    return reaped
