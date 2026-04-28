"""Tests for corruption handling in load_mined_session_state.

Covers:
- UnicodeDecodeError on binary garbage (disk corruption)
- All-lines-invalid treated as catastrophically corrupt
- Partial corruption: valid lines load, bad lines skipped, no backup
- Missing file: returns empty, no backup
- OSError on read: returns empty
"""

import importlib
import json
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def reload_session_state(tmp_cortex_dir):
    """Reload session_state after tmp_cortex_dir sets MEMEM_DIR so that
    MINED_SESSIONS_FILE points to the isolated tmp directory."""
    from memem import session_state
    importlib.reload(session_state)
    return session_state


def _session_state_module():
    from memem import session_state
    return session_state


def test_corrupt_binary_file_backed_up_and_returns_empty(tmp_cortex_dir):
    """Binary garbage triggers UnicodeDecodeError; file backed up, {} returned."""
    ss = _session_state_module()

    # Write 100 bytes of binary garbage
    binary_garbage = bytes(range(256))[:100]
    ss.MINED_SESSIONS_FILE.write_bytes(binary_garbage)

    result = ss.load_mined_session_state()

    assert result == {}
    backups = list(ss.MINED_SESSIONS_FILE.parent.glob(".mined_sessions.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == binary_garbage


def test_all_lines_invalid_json_treated_as_corrupt(tmp_cortex_dir):
    """Non-empty file where every line fails per-line parse is treated as corrupt."""
    ss = _session_state_module()

    ss.MINED_SESSIONS_FILE.write_text("this is not json\nand neither is this\n")

    result = ss.load_mined_session_state()

    assert result == {}
    backups = list(ss.MINED_SESSIONS_FILE.parent.glob(".mined_sessions.corrupt.*"))
    assert len(backups) == 1


def test_partial_corruption_preserves_valid_lines(tmp_cortex_dir):
    """One good JSON line + one garbage line: good line loads, no backup created."""
    ss = _session_state_module()

    good_state = {
        "session_id": "abc123",
        "status": "complete",
        "mtime_ns": 1000,
        "size": 500,
        "version": "1",
        "updated_at": "2026-01-01",
        "message": "",
    }
    content = json.dumps(good_state) + "\nnot-json-garbage\n"
    ss.MINED_SESSIONS_FILE.write_text(content)

    result = ss.load_mined_session_state()

    assert "abc123" in result
    assert result["abc123"]["status"] == "complete"
    # No backup — partial corruption is normal per-line skipping
    backups = list(ss.MINED_SESSIONS_FILE.parent.glob(".mined_sessions.corrupt.*"))
    assert len(backups) == 0


def test_missing_file_returns_empty_no_backup(tmp_cortex_dir):
    """Non-existent file returns {} without creating a backup."""
    ss = _session_state_module()

    assert not ss.MINED_SESSIONS_FILE.exists()
    result = ss.load_mined_session_state()

    assert result == {}
    backups = list(ss.MINED_SESSIONS_FILE.parent.glob(".mined_sessions.corrupt.*"))
    assert len(backups) == 0


def test_oserror_returns_empty(tmp_cortex_dir):
    """OSError on read_text returns {} without crashing."""
    ss = _session_state_module()

    # Create the file so the exists() check passes
    ss.MINED_SESSIONS_FILE.write_text("placeholder")

    with patch.object(
        ss.MINED_SESSIONS_FILE.__class__,
        "read_text",
        side_effect=OSError("permission denied"),
    ):
        result = ss.load_mined_session_state()

    assert result == {}
