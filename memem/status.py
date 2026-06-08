"""
memem status renderer — v2.1.0 event-triggered mining.

Exposes render_status() -> str which builds the full status report.
Daemon-based sections removed; replaced with event-triggered mining info.
"""

import os
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
# [1] Recent mining activity
# ---------------------------------------------------------------------------

def _render_recent_activity(lines: list[str]) -> None:
    """Append [1] Recent mining activity section to lines."""
    lines.append("[1] Recent mining activity (last 5 sessions)")

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
# [2] Mining (event-triggered)
# ---------------------------------------------------------------------------

def _find_hooks_json() -> Path | None:
    """Locate hooks.json by checking CLAUDE_PLUGIN_ROOT and fallback paths."""
    # 1. Try env var
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if plugin_root:
        candidate = Path(plugin_root) / "hooks" / "hooks.json"
        if candidate.exists():
            return candidate

    # 2. Try marketplace glob (no hardcoded developer paths — those leak between
    #    machines and create false positives reading another user's hooks.json.)
    import glob  # noqa: PLC0415
    pattern = str(Path.home() / ".claude/plugins/cache/memem-marketplace/memem/*/hooks/hooks.json")
    matches = glob.glob(pattern)
    if matches:
        return Path(sorted(matches)[-1])  # pick latest version

    return None


def _memem_state_dir() -> Path:
    """Resolve the memem state dir honoring MEMEM_DIR / CORTEX_DIR env vars.

    Lazy resolution (no module-level cache) so test fixtures that override the
    env vars per-test see the right value.
    """
    return Path(os.environ.get("MEMEM_DIR") or os.environ.get("CORTEX_DIR") or (Path.home() / ".memem"))


def _render_mining_event_triggered(lines: list[str]) -> None:
    """Append [2] Mining (event-triggered) section to lines."""
    lines.append("[2] Mining (event-triggered)")

    # 1. Stop hook registered?
    hooks_path = _find_hooks_json()
    if hooks_path is not None:
        try:
            content = hooks_path.read_text()
            if "stop-mine.sh" in content:
                lines.append("  Stop hook:       registered")
            else:
                lines.append("  Stop hook:       NOT registered (hooks.json found but stop-mine.sh missing)")
        except OSError as exc:
            lines.append(f"  Stop hook:       (error reading hooks.json: {exc})")
    else:
        lines.append("  Stop hook:       NOT registered (hooks.json not found)")

    # 2. Opt-in marker (honor MEMEM_DIR)
    state_dir = _memem_state_dir()
    opted_in_path = state_dir / ".miner-opted-in"
    if opted_in_path.exists():
        lines.append(f"  Opt-in marker:   present ({opted_in_path})")
    else:
        lines.append(f"  Opt-in marker:   MISSING ({opted_in_path} not found)")

    # 3. Last mine_delta invocation
    mined_sessions_path = state_dir / ".mined_sessions"
    if mined_sessions_path.exists():
        try:
            mtime = mined_sessions_path.stat().st_mtime
            age_secs = int(time.time() - mtime)
            if age_secs < 3600:
                age_str = f"{age_secs // 60} min ago"
            elif age_secs < 86400:
                age_str = f"{age_secs // 3600}h {(age_secs % 3600) // 60}m ago"
            else:
                days = age_secs // 86400
                age_str = f"{days}d ago"
            lines.append(f"  Last mine run:   {age_str}")
        except OSError as exc:
            lines.append(f"  Last mine run:   (error: {exc})")

        # 4. Count of mined sessions
        try:
            text = mined_sessions_path.read_text()
            count = len([ln for ln in text.splitlines() if ln.strip()])
            lines.append(f"  Sessions mined:  {count} total")
        except OSError as exc:
            lines.append(f"  Sessions mined:  (error: {exc})")
    else:
        lines.append("  Last mine run:   never")
        lines.append("  Sessions mined:  0")

    lines.append("")


# ---------------------------------------------------------------------------
# [3] Per-session attempts
# ---------------------------------------------------------------------------

def _render_per_session_attempts(lines: list[str]) -> None:
    """Append [3] Per-session attempts section to lines."""
    try:
        from memem.session_state import (  # noqa: PLC0415
            STATUS_BLOCKED,
            STATUS_FAILED,
            STATUS_RETRYING,
            load_mined_session_state,
        )

        all_states = load_mined_session_state()
        in_progress = [
            (sid, s) for sid, s in all_states.items()
            if s.get("status") in (STATUS_RETRYING, STATUS_FAILED, STATUS_BLOCKED)
            and int(s.get("attempts", 0)) > 0
        ]
        in_progress.sort(key=lambda x: int(x[1].get("attempts", 0)), reverse=True)
        if in_progress:
            lines.append("[3] Per-session attempts (top 5 by attempts):")
            for sid, s in in_progress[:5]:
                msg = s.get("message", "")[:60]
                lines.append(
                    f"  {sid[:12]}: status={s.get('status')} attempts={s.get('attempts')} last_error={msg!r}"
                )
            lines.append("")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[3] Per-session: error reading state DB: {exc}")
        lines.append("")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_status() -> str:
    """Build and return the full memem miner status report as a string."""
    lines: list[str] = []

    # Header
    lines.append("memem miner status")
    lines.append("=" * 18)
    lines.append("")

    # --- Sections ---
    _render_recent_activity(lines)
    _render_mining_event_triggered(lines)
    _render_per_session_attempts(lines)

    return "\n".join(lines)
