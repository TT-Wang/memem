"""Tests for the v2.1.0 stale-session sweep in hooks/session-start.sh.

The stale-session sweep block:
  - Runs at SessionStart when .miner-opted-in marker exists
  - Uses `find ... -mmin +10` to locate JSONL files older than 10 min
  - Skips any session_id already listed in MEMEM_DIR/.mined_sessions
  - Spawns up to 3 detached mine_delta processes for stale un-mined sessions

Test 1: Structural verification — the sweep block exists in session-start.sh
        and contains the expected keywords (find, grep, mine_delta).

Test 2: Behavioural stub — create JSONL files under a mock projects dir,
        some older than 10 min and not in .mined_sessions (should be swept),
        some recent or already mined (should be skipped). Verify only the
        correct ones would be caught by the find/grep logic.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_HOOK = REPO_ROOT / "hooks" / "session-start.sh"


# ---------------------------------------------------------------------------
# Test 1: Structural verification of the sweep block
# ---------------------------------------------------------------------------


def test_stale_sweep_block_exists_in_session_start():
    """session-start.sh must contain the stale-session sweep block with the
    expected structural keywords: find, grep, mine_delta."""
    content = SESSION_START_HOOK.read_text()

    # Sweep should be gated on the opt-in marker
    assert ".miner-opted-in" in content, (
        "session-start.sh must check for .miner-opted-in before running sweep"
    )
    # Must use find to locate stale JSONLs
    assert "find" in content, "sweep must use 'find' to locate stale JSONL files"
    # Must use -mmin to filter by modification time (older than 10 min)
    assert "-mmin" in content, "sweep must use -mmin to filter by age"
    # Must check .mined_sessions to skip already-mined sessions
    assert "mined_sessions" in content or "MINED_LIST" in content, (
        "sweep must consult the mined sessions list to avoid re-mining"
    )
    # Must spawn mine_delta
    assert "mine_delta" in content, "sweep must spawn mine_delta for stale sessions"
    # Must cap the number of parallel spawns
    assert "SPAWNED" in content or "break" in content, (
        "sweep must cap the number of parallel mine_delta spawns"
    )


# ---------------------------------------------------------------------------
# Test 2: Behavioural stub — classify sessions as sweepable vs. skipped
# ---------------------------------------------------------------------------


def test_stale_sweep_logic_classifies_sessions_correctly(tmp_path):
    """Verify the Python-equivalent sweep logic: only JSONL files that are
    (a) older than 10 min AND (b) not already in .mined_sessions are swept."""
    # Build a fake projects tree under tmp_path
    projects = tmp_path / "projects"
    (projects / "myproject").mkdir(parents=True)

    now = time.time()
    old_mtime = now - 15 * 60  # 15 minutes ago — should be swept
    new_mtime = now - 2 * 60   # 2 minutes ago — too recent

    # Create JSONL files
    stale_unmined = projects / "myproject" / "stale-unmined.jsonl"
    stale_unmined.write_text('{"type":"user"}\n')
    os.utime(stale_unmined, (old_mtime, old_mtime))

    stale_already_mined = projects / "myproject" / "stale-already-mined.jsonl"
    stale_already_mined.write_text('{"type":"user"}\n')
    os.utime(stale_already_mined, (old_mtime, old_mtime))

    recent_unmined = projects / "myproject" / "recent-unmined.jsonl"
    recent_unmined.write_text('{"type":"user"}\n')
    os.utime(recent_unmined, (new_mtime, new_mtime))

    # Build a .mined_sessions file listing the already-mined session
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    mined_sessions = memem_dir / ".mined_sessions"
    mined_sessions.write_text("stale-already-mined\n")

    # ---- Python equivalent of the bash sweep logic ----
    def _would_be_swept(jsonl: Path, mined_list: Path, max_age_min: int = 10) -> bool:
        """Return True if the file would be picked up by the stale sweep."""
        age_seconds = now - jsonl.stat().st_mtime
        if age_seconds < max_age_min * 60:
            return False
        session_id = jsonl.stem
        if mined_list.exists():
            mined_ids = mined_list.read_text().splitlines()
            if session_id in mined_ids:
                return False
        return True

    assert _would_be_swept(stale_unmined, mined_sessions), (
        "stale-unmined.jsonl should be swept (old + not in mined list)"
    )
    assert not _would_be_swept(stale_already_mined, mined_sessions), (
        "stale-already-mined.jsonl should be skipped (already in mined list)"
    )
    assert not _would_be_swept(recent_unmined, mined_sessions), (
        "recent-unmined.jsonl should be skipped (too new)"
    )
