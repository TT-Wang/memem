"""
memem status renderer — extracted from miner_daemon.status_daemon().

Exposes render_status() -> str which builds the full status report.
status_daemon() in miner_daemon.py is a thin shim that calls this + prints.

Design: no new hard dependencies. Uses stdlib + imports from miner_daemon
for shared constants (HEARTBEAT_FILE, GLOBAL_LOCK_FILE, PID_FILE, LOG_FILE).
Process introspection uses /proc on Linux with graceful skip elsewhere.
"""

import json
import os
import platform
import sqlite3
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_age(seconds: int) -> str:
    """Return a human-friendly age string like '2h 3m' or '45s'."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def _pid_alive(pid: int) -> bool:
    """Return True if pid is alive (kill -0)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


# ---------------------------------------------------------------------------
# [1] Process tree
# ---------------------------------------------------------------------------

def _read_proc_cmdline(pid: int) -> str:
    """Read /proc/<pid>/cmdline and return space-joined string."""
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_proc_stat(pid: int) -> dict:
    """Parse /proc/<pid>/stat for PPID and starttime."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        # Format: pid (comm) state ppid ...
        # comm may contain spaces and parens, so parse carefully
        rp = stat_text.rfind(")")
        if rp == -1:
            return {}
        rest = stat_text[rp + 2:].split()
        # Fields after ')': state ppid pgrp ...
        ppid = int(rest[1]) if len(rest) > 1 else 0
        starttime_jiffies = int(rest[19]) if len(rest) > 19 else 0
        return {"ppid": ppid, "starttime_jiffies": starttime_jiffies}
    except (OSError, ValueError, IndexError):
        return {}


def _read_proc_status_rss(pid: int) -> int:
    """Return RSS in bytes from /proc/<pid>/status VmRSS field."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    kb = int(parts[1])
                    return kb * 1024
    except (OSError, ValueError):
        pass
    return 0


def _get_boot_time() -> float:
    """Return system boot time as Unix timestamp."""
    try:
        stat = Path("/proc/stat").read_text()
        for line in stat.splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _get_all_pids() -> list[int]:
    """Return all visible PIDs from /proc."""
    pids = []
    try:
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                pids.append(int(entry.name))
    except OSError:
        pass
    return pids


def _process_age_seconds(pid: int, boot_time: float, hz: int = 100) -> int:
    """Return process age in seconds."""
    stat = _read_proc_stat(pid)
    if not stat:
        return 0
    starttime_jiffies = stat.get("starttime_jiffies", 0)
    start_ts = boot_time + starttime_jiffies / hz
    return max(0, int(time.time() - start_ts))


def _render_process_tree(lines: list[str]) -> None:
    """Append [1] Process tree section to lines."""
    lines.append("[1] Process tree")

    is_linux = platform.system() == "Linux"
    if not is_linux:
        lines.append("  (process introspection only available on Linux)")
        lines.append("")
        return

    # Import constants from miner_daemon at call time to pick up test monkeypatches
    from memem import miner_daemon  # noqa: PLC0415
    pid_file = miner_daemon.PID_FILE

    boot_time = _get_boot_time()
    hz = 100  # standard Linux jiffies/second

    # Read daemon PID from pid file
    daemon_pid: int | None = None
    try:
        if pid_file.exists():
            daemon_pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pass

    all_pids = _get_all_pids()

    # Detect wrapper: bash .*miner-wrapper.sh or similar
    wrapper_pid: int | None = None
    wrapper_age: int = 0
    for pid in all_pids:
        cmd = _read_proc_cmdline(pid)
        if "miner-wrapper.sh" in cmd and "bash" in cmd:
            wrapper_pid = pid
            wrapper_age = _process_age_seconds(pid, boot_time, hz)
            break

    # Daemon detection: python.* -m memem.miner_daemon
    detected_daemon_pid: int | None = None
    detected_daemon_age: int = 0
    for pid in all_pids:
        cmd = _read_proc_cmdline(pid)
        if "miner_daemon" in cmd and ("python" in cmd.lower()):
            detected_daemon_pid = pid
            detected_daemon_age = _process_age_seconds(pid, boot_time, hz)
            break

    # Show wrapper
    if wrapper_pid:
        age_str = _format_age(wrapper_age)
        lines.append(f"  Wrapper:  running (PID {wrapper_pid}, age {age_str})")
    else:
        lines.append("  Wrapper:  not running")

    # Show daemon — prefer PID file, fall back to detected
    effective_daemon_pid = daemon_pid or detected_daemon_pid
    if effective_daemon_pid:
        alive = _pid_alive(effective_daemon_pid)
        if alive:
            age = detected_daemon_age if effective_daemon_pid == detected_daemon_pid else (
                _process_age_seconds(effective_daemon_pid, boot_time, hz)
            )
            age_str = _format_age(age)
            lines.append(f"  Daemon:   running (PID {effective_daemon_pid}, age {age_str})")
        else:
            lines.append(f"  Daemon:   dead (PID {effective_daemon_pid} in pid file but not alive)")
    else:
        lines.append("  Daemon:   not running")

    # Detect orphan claude -p haiku procs: ppid==1, contains 'claude' + '-p' + 'haiku', age > 60s
    orphans = []
    for pid in all_pids:
        cmd = _read_proc_cmdline(pid)
        if "claude" in cmd and "-p" in cmd and "haiku" in cmd:
            stat = _read_proc_stat(pid)
            ppid = stat.get("ppid", 0)
            if ppid == 1:
                age = _process_age_seconds(pid, boot_time, hz)
                if age > 60:
                    rss_bytes = _read_proc_status_rss(pid)
                    rss_mb = rss_bytes // (1024 * 1024)
                    orphans.append((pid, age, rss_mb))

    if orphans:
        orphans.sort(key=lambda x: x[1], reverse=True)
        top5 = orphans[:5]
        lines.append(f"  Orphan claude -p haiku procs: {len(orphans)}  ⚠  (PPID=1, age >60s)")
        for pid, age, rss_mb in top5:
            lines.append(f"    - PID {pid}  age {age}s  rss {rss_mb}MB")

    lines.append("")


# ---------------------------------------------------------------------------
# [2] Heartbeat
# ---------------------------------------------------------------------------

def _render_heartbeat(lines: list[str]) -> None:
    """Append [2] Heartbeat section to lines."""
    lines.append("[2] Heartbeat")

    from memem import miner_daemon  # noqa: PLC0415
    hb_file = miner_daemon.HEARTBEAT_FILE

    if hb_file.exists():
        try:
            mtime = hb_file.stat().st_mtime
            age = int(time.time() - mtime)
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
            if age < 60:
                band = "✓ OK"
            elif age < 300:
                band = "⚠ WARN"
            else:
                band = "✗ FAIL"
            lines.append(f"  Last write: {ts_iso}  (age {age}s)  {band}")
        except OSError as exc:
            lines.append(f"  (error reading heartbeat file: {exc})")
    else:
        lines.append("  Last write: no heartbeat file (missing — daemon may never have started)")

    lines.append("  Status:     ✓ OK <60s   |   ⚠ WARN <300s   |   ✗ FAIL ≥300s")
    lines.append("")


# ---------------------------------------------------------------------------
# [3] Lock file
# ---------------------------------------------------------------------------

def _render_lock_file(lines: list[str]) -> None:
    """Append [3] Lock file section to lines."""
    lines.append("[3] Lock file")

    from memem import miner_daemon  # noqa: PLC0415
    lock_file = miner_daemon.GLOBAL_LOCK_FILE

    lines.append(f"  Path:    {lock_file}")

    if lock_file.exists():
        try:
            raw = lock_file.read_text().strip()
            # Lock file may contain PID on first line
            lock_pid_str = raw.splitlines()[0].strip() if raw else ""
            if lock_pid_str.isdigit():
                lock_pid = int(lock_pid_str)
                if _pid_alive(lock_pid):
                    # Check if it matches the daemon PID
                    daemon_pid: int | None = None
                    try:
                        if miner_daemon.PID_FILE.exists():
                            daemon_pid = int(miner_daemon.PID_FILE.read_text().strip())
                    except (OSError, ValueError):
                        pass

                    if daemon_pid and lock_pid == daemon_pid:
                        lines.append(f"  Owner:   PID {lock_pid}  ✓ alive (matches daemon)")
                    else:
                        lines.append(f"  Owner:   PID {lock_pid}  ✓ alive")
                else:
                    lines.append(
                        f"  Owner:   PID {lock_pid}  ✗ STALE (PID dead — would block restart; consider cleanup)"
                    )
            else:
                lines.append(f"  Owner:   (unparseable lock content: {raw[:40]!r})")
        except (OSError, IndexError) as exc:
            lines.append(f"  Owner:   (error reading lock file: {exc})")
    else:
        lines.append("  Owner:   (no lock file present)")

    lines.append("")


# ---------------------------------------------------------------------------
# [4] Recent mining activity
# ---------------------------------------------------------------------------

def _render_recent_activity(lines: list[str]) -> None:
    """Append [4] Recent mining activity section to lines."""
    lines.append("[4] Recent mining activity (last 5 sessions)")

    from memem.session_state import _db_path  # noqa: PLC0415

    db_path = _db_path()
    if not db_path.exists():
        lines.append("  (no mined_sessions.db yet)")
        lines.append("")
        return

    try:
        conn = sqlite3.connect(str(db_path), timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            # Fetch last 5 rows ordered by updated_at desc
            cursor = conn.execute(
                "SELECT session_id, status, attempts, message, updated_at"
                " FROM mined_sessions"
                " ORDER BY updated_at DESC LIMIT 5"
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            lines.append("  (no sessions recorded yet)")
        else:
            # Header
            lines.append(f"  {'session_id':<14} {'status':<12} {'memories_saved':>14}  {'attempts':>8}")
            lines.append(f"  {'-'*14} {'-'*12} {'-'*14}  {'-'*8}")
            for row in rows:
                sid = str(row["session_id"])[:12]
                status = str(row["status"])
                attempts = row["attempts"]
                # memories_saved not in schema — show dash
                lines.append(f"  {sid:<14} {status:<12} {'—':>14}  {attempts:>8}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  (error reading session DB: {exc})")

    lines.append("")


# ---------------------------------------------------------------------------
# [5] Recursion-orphan counter
# ---------------------------------------------------------------------------

def _render_orphan_counter(lines: list[str]) -> None:
    """Append [5] Recursion-orphan counter section to lines."""
    lines.append("[5] Recursion-orphan counter")

    from memem import miner_daemon  # noqa: PLC0415
    log_file = miner_daemon.LOG_FILE

    if not log_file.exists():
        lines.append("  Total claude -p haiku procs reaped since daemon start: no data yet (no log file)")
        lines.append("  Last reap sweep: no data yet")
        lines.append("")
        return

    try:
        # Read last 10k lines of log file
        all_lines = log_file.read_text(errors="replace").splitlines()
        tail = all_lines[-10000:]

        # Find most-recent mining_loop_started event as anchor
        anchor_idx: int = -1
        for i in range(len(tail) - 1, -1, -1):
            try:
                obj = json.loads(tail[i])
                if obj.get("event") == "mining_loop_started":
                    anchor_idx = i
                    break
            except (json.JSONDecodeError, AttributeError):
                continue

        # Count orphan_reaped events since anchor
        search_range = tail[anchor_idx + 1:] if anchor_idx >= 0 else tail
        orphan_reaped_count = 0
        for raw_line in search_range:
            try:
                obj = json.loads(raw_line)
                if obj.get("event") == "orphan_reaped":
                    orphan_reaped_count += 1
            except (json.JSONDecodeError, AttributeError):
                continue

        lines.append(
            f"  Total claude -p haiku procs reaped since daemon start: {orphan_reaped_count}"
            f" (from log events 'orphan_reaped')"
        )

        # Find most-recent orphan_reaper_sweep event
        last_sweep_ts: str | None = None
        last_sweep_reaped: int | None = None
        for raw_line in reversed(tail):
            try:
                obj = json.loads(raw_line)
                if obj.get("event") == "orphan_reaper_sweep":
                    last_sweep_ts = obj.get("timestamp") or obj.get("ts") or "unknown"
                    last_sweep_reaped = obj.get("reaped_count", 0)
                    break
            except (json.JSONDecodeError, AttributeError):
                continue

        if last_sweep_ts is not None:
            lines.append(f"  Last reap sweep: {last_sweep_ts}  reaped {last_sweep_reaped}")
        else:
            lines.append("  Last reap sweep: no data yet")

    except OSError as exc:
        lines.append(f"  (error reading log file: {exc})")

    lines.append("")


# ---------------------------------------------------------------------------
# [6] Slice daemon health (v1.8.1)
# ---------------------------------------------------------------------------

def _render_slice_daemon(lines: list[str]) -> None:
    """Section [6] — health of the persistent slice daemon (v1.8.0+).

    Reports PID, socket, heartbeat age in OK/WARN/FAIL bands matching the
    miner heartbeat section.
    """
    lines.append("[6] Slice daemon")

    try:
        from memem import slice_daemon  # noqa: PLC0415
    except Exception as exc:
        lines.append(f"  ✗ slice_daemon module not importable: {exc}")
        lines.append("")
        return

    pid_file = slice_daemon.PID_FILE
    sock_file = slice_daemon.SOCK_FILE
    hb_file = slice_daemon.HEARTBEAT_FILE

    pid: int | None = None
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pass

    if not pid:
        lines.append("  Daemon:    not running (start with `python -m memem.slice_daemon start`)")
        lines.append("  Impact:    hooks fall back to cold-subprocess slice generation (~5-10s/prompt)")
        lines.append("")
        return

    alive = _pid_alive(pid)
    status_glyph = "✓" if alive else "✗ STALE PID"
    lines.append(f"  Daemon:    {status_glyph} PID {pid}")

    if sock_file.exists():
        try:
            mode = sock_file.stat().st_mode & 0o777
            perm_ok = "✓" if mode == 0o600 else f"⚠ mode={oct(mode)}"
            lines.append(f"  Socket:    {sock_file}  ({perm_ok})")
        except OSError as exc:
            lines.append(f"  Socket:    {sock_file}  ({exc})")
    else:
        lines.append(f"  Socket:    missing — {sock_file}")

    if hb_file.exists():
        try:
            mtime = hb_file.stat().st_mtime
            age = int(time.time() - mtime)
            if age < 60:
                band = "✓ OK"
            elif age < 300:
                band = "⚠ WARN"
            else:
                band = "✗ FAIL"
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
            lines.append(f"  Heartbeat: {ts_iso} (age {age}s)  {band}")
        except OSError as exc:
            lines.append(f"  Heartbeat: error reading {hb_file}: {exc}")
    else:
        lines.append("  Heartbeat: (no requests served yet)")
    lines.append("")


# ---------------------------------------------------------------------------
# Legacy sections (preserved from original status_daemon for backward compat)
# ---------------------------------------------------------------------------

def _render_legacy_sections(lines: list[str]) -> None:
    """Append the original circuit breaker + per-session + log sections.

    These were present in the old status_daemon() output. Keep them so
    existing users and scripts that grep for 'Circuit breaker:',
    'Per-session attempts', or 'Recent log' continue to work.
    """
    from memem import miner_daemon  # noqa: PLC0415

    # Circuit breaker
    lines.append("Circuit breaker:")
    info = miner_daemon._circuit_breaker.state_info()
    lines.append(f"  state:                 {info['state']}")
    lines.append(f"  consecutive_failures:  {info['consecutive_failures']}")
    lines.append(f"  failure_threshold:     {info['failure_threshold']}")
    lines.append("")

    # Per-session attempts
    try:
        from memem.miner_protocol import STATUS_BLOCKED, STATUS_FAILED, STATUS_RETRYING  # noqa: PLC0415
        from memem.session_state import load_mined_session_state  # noqa: PLC0415

        all_states = load_mined_session_state()
        in_progress = [
            (sid, s) for sid, s in all_states.items()
            if s.get("status") in (STATUS_RETRYING, STATUS_FAILED, STATUS_BLOCKED)
            and int(s.get("attempts", 0)) > 0
        ]
        in_progress.sort(key=lambda x: int(x[1].get("attempts", 0)), reverse=True)
        if in_progress:
            lines.append("Per-session attempts (top 5 by attempts):")
            for sid, s in in_progress[:5]:
                msg = s.get("message", "")[:60]
                lines.append(
                    f"  {sid[:12]}: status={s.get('status')} attempts={s.get('attempts')} last_error={msg!r}"
                )
            lines.append("")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Per-session: error reading state DB: {exc}")
        lines.append("")

    # Recent log tail
    lines.append("Recent log (last 20 lines):")
    log_file = miner_daemon.LOG_FILE
    if log_file.exists():
        try:
            log_lines = log_file.read_text().splitlines()
            for line in log_lines[-20:]:
                lines.append(f"  {line}")
        except OSError as exc:
            lines.append(f"  (error reading log: {exc})")
    else:
        lines.append("  (no log file)")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_status() -> str:
    """Build and return the full memem miner status report as a string."""
    from memem import miner_daemon  # noqa: PLC0415

    lines: list[str] = []

    # Header
    lines.append("memem miner status")
    lines.append("=" * 18)
    lines.append("")

    # Daemon line (simple, from PID file — preserved from original)
    pid_file = miner_daemon.PID_FILE
    daemon_pid: int | None = None
    try:
        if pid_file.exists():
            daemon_pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pass

    if daemon_pid:
        lines.append(f"Daemon:    running (PID {daemon_pid})")
    else:
        lines.append("Daemon:    not running")
    lines.append("")

    # Heartbeat (original short form for backward compat — kept before new sections)
    hb_file = miner_daemon.HEARTBEAT_FILE
    if hb_file.exists():
        try:
            mtime = hb_file.stat().st_mtime
            age = int(time.time() - mtime)
            ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
            lines.append(f"Heartbeat: {ts_iso} ({age}s ago)")
        except OSError:
            lines.append("Heartbeat: (error reading file)")
    else:
        lines.append("Heartbeat: missing (daemon never started or never wrote)")
    lines.append("")

    # Lock owner (original short form for backward compat)
    lock_file = miner_daemon.GLOBAL_LOCK_FILE
    if lock_file.exists():
        try:
            lock_pid_str = lock_file.read_text().strip().splitlines()[0]
            lines.append(f"Lock:      held by PID {lock_pid_str} ({lock_file})")
        except (OSError, IndexError):
            lines.append(f"Lock:      file present but unreadable ({lock_file})")
    else:
        lines.append("Lock:      no lockfile present")
    lines.append("")

    # --- New sections ---
    _render_process_tree(lines)
    _render_heartbeat(lines)
    _render_lock_file(lines)
    _render_recent_activity(lines)
    _render_orphan_counter(lines)
    _render_slice_daemon(lines)  # v1.8.1

    # --- Legacy sections (circuit breaker, per-session, log tail) ---
    _render_legacy_sections(lines)

    return "\n".join(lines)
