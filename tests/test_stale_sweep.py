"""Tests for the v2.1.0 stale-session sweep in hooks/session-start.sh.

The stale-session sweep block:
  - Runs at SessionStart when .miner-opted-in marker exists
  - Uses `find ... -mmin +10` to locate JSONL files older than 10 min
  - Skips any session_id already listed in MEMEM_DIR/.mined_sessions
  - Spawns up to 3 detached mine_delta processes for stale un-mined sessions
  - SKIPS transcripts whose first 20 lines contain mining artifact markers
    ('=== BEGIN CONVERSATION ===' or 'Below is a coding conversation')
  - Appends skipped mining artifact session IDs to .mined_sessions (zombie guard)

Test 1: Structural verification — the sweep block exists in session-start.sh
        and contains the expected keywords (find, grep, mine_delta).

Test 2: Behavioural stub — create JSONL files under a mock projects dir,
        some older than 10 min and not in .mined_sessions (should be swept),
        some recent or already mined (should be skipped). Verify only the
        correct ones would be caught by the find/grep logic.

Test 3: Mining artifact detection — JSONL files with mining markers in the
        first user message are SKIPPED (not spawned) by the sweep.

Test 4: Zombie-session guard — a skipped mining artifact gets appended to
        .mined_sessions so it is never re-examined on future SessionStart.

Test 5: Structural check — the hook must contain the mining-artifact guard
        lines (BEGIN CONVERSATION / Below is a coding conversation).
"""

from __future__ import annotations

import json
import os
import subprocess
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


# ---------------------------------------------------------------------------
# Helpers: write realistic JSONL fixtures using Claude Code's nested schema
# {"type":"user","message":{"role":"user","content":"..."}}
# ---------------------------------------------------------------------------

def _write_real_user_jsonl(path: Path, content: str) -> None:
    """Write a JSONL file with a real Claude Code user message as its first user entry."""
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
        "uuid": "test-uuid-0001",
        "timestamp": "2025-01-01T00:00:00.000Z",
    })
    path.write_text(line + "\n")


def _write_real_user_jsonl_blocks(path: Path, text: str) -> None:
    """Write a JSONL file with content as a list of text blocks (alternative schema)."""
    line = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
        "uuid": "test-uuid-0002",
        "timestamp": "2025-01-01T00:00:00.000Z",
    })
    path.write_text(line + "\n")


# ---------------------------------------------------------------------------
# Test 3: Mining artifact detection — bash-level grep
# ---------------------------------------------------------------------------

_ARTIFACT_MARKERS = [
    "=== BEGIN CONVERSATION ===",
    "Below is a coding conversation",
]


_ARTIFACT_MAX_LINES = 30  # mirrors the `wc -l <= 30` conjunct in session-start.sh


def _is_mining_artifact(jsonl: Path) -> bool:
    """Python equivalent of the bash mining-artifact check:
    marker in the first 20 lines AND total line count <= 30 (headless
    `claude -p` transcripts are tiny; real sessions quoting a marker are not).
    """
    try:
        all_lines = jsonl.read_text(errors="replace").splitlines()
        if len(all_lines) > _ARTIFACT_MAX_LINES:
            return False
        text = "\n".join(all_lines[:20])
        return any(marker in text for marker in _ARTIFACT_MARKERS)
    except OSError:
        return False


def test_mining_artifact_begin_conversation_detected(tmp_path):
    """A JSONL whose first user message contains '=== BEGIN CONVERSATION ===' is detected."""
    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)
    mining_jsonl = projects / "mining-session-abc.jsonl"
    _write_real_user_jsonl(
        mining_jsonl,
        "=== BEGIN CONVERSATION ===\nUser: hello\nAssistant: world\n=== END CONVERSATION ===",
    )
    assert _is_mining_artifact(mining_jsonl), (
        "JSONL with '=== BEGIN CONVERSATION ===' must be detected as mining artifact"
    )


def test_mining_artifact_below_coding_conversation_detected(tmp_path):
    """A JSONL whose first user message contains 'Below is a coding conversation' is detected."""
    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)
    mining_jsonl = projects / "mining-session-def.jsonl"
    _write_real_user_jsonl(
        mining_jsonl,
        "Below is a coding conversation (human messages and assistant prose, with tool calls stripped).\n\n=== BEGIN CONVERSATION ===\n",
    )
    assert _is_mining_artifact(mining_jsonl), (
        "JSONL with 'Below is a coding conversation' must be detected as mining artifact"
    )


def test_mining_artifact_content_as_blocks_detected(tmp_path):
    """Detection works when content is a list of text blocks (alternative JSONL schema)."""
    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)
    mining_jsonl = projects / "mining-session-blocks.jsonl"
    _write_real_user_jsonl_blocks(
        mining_jsonl,
        "=== BEGIN CONVERSATION ===\nUser: something\n=== END CONVERSATION ===",
    )
    assert _is_mining_artifact(mining_jsonl), (
        "Marker detection must work when content is stored as block list"
    )


def test_normal_session_not_detected_as_artifact(tmp_path):
    """A normal user JSONL is NOT misidentified as a mining artifact."""
    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)
    normal_jsonl = projects / "normal-session-ghi.jsonl"
    _write_real_user_jsonl(normal_jsonl, "How do I use pytest fixtures?")
    assert not _is_mining_artifact(normal_jsonl), (
        "Normal session JSONL must NOT be flagged as mining artifact"
    )


def test_long_real_session_quoting_marker_not_flagged(tmp_path):
    """False-positive guard: a real (long) conversation that merely QUOTES a
    mining marker phrase must NOT be flagged. A false positive here would
    permanently exclude the session from auto-mining (zombie guard appends it
    to .mined_sessions) — silent memory loss. The line-count conjunct
    (<= 30 lines) is what protects this case: headless mining transcripts are
    tiny, real conversations are not."""
    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)
    long_jsonl = projects / "real-session-quoting-marker.jsonl"

    lines = [
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": 'What does "Below is a coding conversation" mean in memem\'s mining prompt?',
            },
            "uuid": "fp-uuid-0000",
            "timestamp": "2025-01-01T00:00:00.000Z",
        })
    ]
    # 39 follow-up turns — a real conversation's length, far above the
    # _ARTIFACT_MAX_LINES threshold.
    for i in range(39):
        role = "assistant" if i % 2 == 0 else "user"
        lines.append(json.dumps({
            "type": role,
            "message": {"role": role, "content": f"turn {i} discussing the mining system"},
            "uuid": f"fp-uuid-{i + 1:04d}",
            "timestamp": "2025-01-01T00:00:01.000Z",
        }))
    long_jsonl.write_text("\n".join(lines) + "\n")

    assert not _is_mining_artifact(long_jsonl), (
        "A long real session quoting a mining marker must NOT be flagged "
        "(would be permanently excluded from mining)"
    )


# ---------------------------------------------------------------------------
# Test 4: Zombie-session guard — skipped artifacts appended to .mined_sessions
# ---------------------------------------------------------------------------

def test_zombie_guard_appends_sid_to_mined_list(tmp_path):
    """When a mining artifact is skipped, its session ID must be appended to
    .mined_sessions so future sweeps don't re-examine it (zombie guard)."""
    memem_dir = tmp_path / ".memem"
    memem_dir.mkdir()
    mined_sessions = memem_dir / ".mined_sessions"
    mined_sessions.write_text("")

    projects = tmp_path / "projects" / "myproj"
    projects.mkdir(parents=True)

    artifact_jsonl = projects / "artifact-session-xyz.jsonl"
    _write_real_user_jsonl(
        artifact_jsonl,
        "=== BEGIN CONVERSATION ===\nUser: hello\n=== END CONVERSATION ===",
    )

    now = time.time()
    old_mtime = now - 15 * 60
    os.utime(artifact_jsonl, (old_mtime, old_mtime))

    # Simulate the bash sweep logic: if it's a mining artifact, append SID to mined_sessions
    def _sweep_step(jsonl: Path, mined_list: Path) -> str:
        """Returns 'mine', 'skip_mined', 'skip_artifact' depending on outcome."""
        sid = jsonl.stem
        if mined_list.exists():
            if sid in mined_list.read_text().splitlines():
                return "skip_mined"
        if _is_mining_artifact(jsonl):
            # Zombie guard: append SID if not already there
            existing = mined_list.read_text().splitlines() if mined_list.exists() else []
            if sid not in existing:
                with open(mined_list, "a") as f:
                    f.write(sid + "\n")
            return "skip_artifact"
        return "mine"

    result = _sweep_step(artifact_jsonl, mined_sessions)
    assert result == "skip_artifact", "Mining artifact must be skipped, not mined"

    # Verify the SID was appended to .mined_sessions
    mined_ids = mined_sessions.read_text().splitlines()
    assert "artifact-session-xyz" in mined_ids, (
        "Skipped mining artifact SID must be appended to .mined_sessions (zombie guard)"
    )

    # Second sweep: now it's in mined list → skip_mined (not skip_artifact again)
    result2 = _sweep_step(artifact_jsonl, mined_sessions)
    assert result2 == "skip_mined", (
        "On re-sweep, artifact must be skipped via mined_sessions list (not re-detected)"
    )


# ---------------------------------------------------------------------------
# Test 5: Structural check for mining-artifact guard in session-start.sh
# ---------------------------------------------------------------------------

def test_session_start_contains_mining_artifact_guard():
    """session-start.sh must contain the mining-artifact detection pattern."""
    content = SESSION_START_HOOK.read_text()
    assert "BEGIN CONVERSATION" in content, (
        "session-start.sh must check for '=== BEGIN CONVERSATION ===' mining marker"
    )
    assert "Below is a coding conversation" in content, (
        "session-start.sh must check for 'Below is a coding conversation' mining marker"
    )
    # Guard must append to MINED_LIST to prevent zombie re-examination
    assert "MINED_LIST" in content, (
        "session-start.sh mining artifact guard must reference MINED_LIST (zombie guard)"
    )
