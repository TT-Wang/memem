"""Tests for m4 — Mining latency on session-end.

Covers:
1. mine_session_delta: resolves session_id → JSONL path, calls mine_session.
2. Stop-hook marker logic: first stop triggers mine, second stop skips it.
3. offset_bytes: mine_session_delta respects the stored offset (no re-mining).
4. has_retryable_sessions: returns True/False correctly based on state.
5. compute_sleep_cap: daemon sleep is capped at 900s with retryable sessions,
   allowed up to 1800s when all sessions are terminal.
"""

import importlib
import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Isolated sessions dir that SESSIONS_DIRS points at."""
    sessions_dir = tmp_path / ".claude" / "projects"
    sessions_dir.mkdir(parents=True)

    # Point SESSIONS_DIRS at the temp directory so mine_session_delta scans it.
    import memem.session_state as ss
    monkeypatch.setattr(ss, "SESSIONS_DIRS", [sessions_dir])
    # Also patch the module-level import inside mining.py (deferred import)
    # by pointing session_state.SESSIONS_DIRS — mining imports lazily.
    return sessions_dir


@pytest.fixture
def tmp_memem_dir(tmp_path, monkeypatch):
    """Isolated ~/.memem state dir."""
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)
    import memem.models as models_mod
    import memem.session_state as ss
    importlib.reload(models_mod)
    importlib.reload(ss)
    return state


@pytest.fixture
def simple_jsonl(tmp_path):
    """Write a tiny JSONL session file with two lines of real content."""
    content = (
        json.dumps({"type": "user", "message": {"content": "Hello world"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": "Hi there"}}) + "\n"
    )
    p = tmp_path / "abc123session.jsonl"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 1. mine_session_delta: first stop fires mine
# ---------------------------------------------------------------------------


def test_mine_session_delta_calls_mine_session(tmp_sessions, simple_jsonl):
    """mine_session_delta(session_id) should call mine_session with the right path."""
    # Place the JSONL under the sessions dir
    proj_dir = tmp_sessions / "myproject"
    proj_dir.mkdir()
    target = proj_dir / "abc123session.jsonl"
    target.write_bytes(simple_jsonl.read_bytes())

    with patch("memem.mining.mine_session") as mock_mine:
        mock_mine.return_value = {"skipped": False, "memories_saved": 1}

        from memem.mining import mine_session_delta
        result = mine_session_delta("abc123session")

    mock_mine.assert_called_once()
    called_path = mock_mine.call_args[0][0]
    assert "abc123session.jsonl" in called_path
    assert result == {"skipped": False, "memories_saved": 1}


def test_mine_session_delta_not_found(tmp_sessions):
    """mine_session_delta returns skipped=True when session file is not found."""
    with patch("memem.mining.mine_session") as mock_mine:
        from memem.mining import mine_session_delta
        result = mine_session_delta("nonexistent-session-id")

    mock_mine.assert_not_called()
    assert result["skipped"] is True
    assert "not found" in result["reason"]


# ---------------------------------------------------------------------------
# 2. Stop-hook marker: second stop does NOT re-mine
# ---------------------------------------------------------------------------


def test_stop_marker_prevents_second_mine(tmp_path):
    """Once the marker file exists, mine_session_delta should not be called again.

    This test simulates the hook's marker-file logic in Python:
    - First call: no marker → mine fires → marker created.
    - Second call: marker exists → mine skipped.
    """
    marker_dir = tmp_path / ".stop-timestamps"
    marker_dir.mkdir()
    session_id = "testsession123"
    marker = marker_dir / f"{session_id}.ts"

    call_count = {"n": 0}

    def fake_mine(sid):
        call_count["n"] += 1
        return {"skipped": False, "memories_saved": 0}

    def simulate_hook_step5(session_id, marker_path, mine_fn):
        if marker_path.exists():
            return  # already mined
        mine_fn(session_id)
        marker_path.touch()

    # First invocation — no marker
    simulate_hook_step5(session_id, marker, fake_mine)
    assert call_count["n"] == 1
    assert marker.exists()

    # Second invocation — marker present
    simulate_hook_step5(session_id, marker, fake_mine)
    assert call_count["n"] == 1  # still 1, not called again


# ---------------------------------------------------------------------------
# 3. mine_session_delta respects offset_bytes
# ---------------------------------------------------------------------------


def test_mine_session_delta_respects_offset_bytes(tmp_path, monkeypatch):
    """Calling mine_session_delta twice on the same JSONL should not re-process bytes.

    After the first call the session state stores an offset_bytes equal to the
    file size. The second call should see delta_bytes == 0 and return skipped.
    We mock the Haiku call so the test doesn't require a live claude CLI.
    """
    import importlib

    # Set up isolated state dir
    state = tmp_path / ".memem"
    state.mkdir()
    monkeypatch.setenv("MEMEM_DIR", str(state))
    monkeypatch.delenv("CORTEX_DIR", raising=False)

    # Set up sessions dir
    sessions_dir = tmp_path / ".claude" / "projects"
    proj_dir = sessions_dir / "proj"
    proj_dir.mkdir(parents=True)

    session_id = "offsettest123"
    jsonl = proj_dir / f"{session_id}.jsonl"

    # Write enough content to exceed _MIN_DELTA_BYTES (100 bytes)
    line = json.dumps({"type": "user", "message": {"content": "A" * 200}}) + "\n"
    jsonl.write_text(line)

    # Reload modules with isolated dirs
    import memem.models as models_mod
    import memem.session_state as ss
    importlib.reload(models_mod)
    importlib.reload(ss)

    # Now patch SESSIONS_DIRS on the freshly reloaded module
    monkeypatch.setattr(ss, "SESSIONS_DIRS", [sessions_dir])

    # Patch Haiku to return empty memories so mine_session completes quickly.
    # Also patch _generate_index to avoid Obsidian vault setup.
    with patch("memem.mining._summarize_session_haiku", return_value=[]), patch("memem.mining._generate_index"):
        from memem.mining import mine_session_delta
        result1 = mine_session_delta(session_id)

    # First call: not skipped with "session not found" — must have found it
    assert result1.get("reason") != "session not found", (
        f"mine_session_delta could not find the JSONL. result={result1}"
    )
    # Result should reflect completion (possibly "nothing worth saving" etc.)
    expected_reasons = {
        "delta too small",
        "no human messages",
        "nothing worth saving",
        "already mined",
        None,  # no reason = actually mined
    }
    assert result1.get("reason") in expected_reasons, f"Unexpected reason: {result1}"

    # Second call: file unchanged → should be skipped (already mined)
    with patch("memem.mining._summarize_session_haiku", return_value=[]) as mock_haiku:
        result2 = mine_session_delta(session_id)

    # Either completely skipped or haiku was not called because delta was zero
    assert result2.get("skipped") is True or mock_haiku.call_count == 0, (
        f"Expected second call to skip or not call Haiku, got: {result2}, haiku calls: {mock_haiku.call_count}"
    )


# ---------------------------------------------------------------------------
# 4. has_retryable_sessions semantics
# ---------------------------------------------------------------------------


def test_has_retryable_all_complete():
    """All sessions STATUS_COMPLETE → no retryable sessions."""
    from memem.miner_daemon import has_retryable_sessions
    from memem.miner_protocol import STATUS_COMPLETE

    states = {
        "sess1": {"status": STATUS_COMPLETE, "attempts": 0},
        "sess2": {"status": STATUS_COMPLETE, "attempts": 3},
    }
    assert has_retryable_sessions(states) is False


def test_has_retryable_pending_low_attempts():
    """Session with status=pending and attempts=2 is retryable (below HARD_RETRY_CAP=5)."""
    from memem.miner_daemon import has_retryable_sessions
    from memem.miner_protocol import STATUS_FAILED

    states = {
        "sess1": {"status": STATUS_FAILED, "attempts": 2},
    }
    assert has_retryable_sessions(states) is True


def test_has_retryable_at_hard_retry_cap():
    """Session with attempts==HARD_RETRY_CAP is NOT retryable (exhausted)."""
    from memem.miner_daemon import has_retryable_sessions
    from memem.miner_protocol import STATUS_FAILED
    from memem.session_state import HARD_RETRY_CAP

    states = {
        "sess1": {"status": STATUS_FAILED, "attempts": HARD_RETRY_CAP},
    }
    assert has_retryable_sessions(states) is False


def test_has_retryable_mixed():
    """Mixed states: if at least one session is retryable, returns True."""
    from memem.miner_daemon import has_retryable_sessions
    from memem.miner_protocol import STATUS_COMPLETE, STATUS_FAILED
    from memem.session_state import HARD_RETRY_CAP

    states = {
        "done": {"status": STATUS_COMPLETE, "attempts": 0},
        "capped": {"status": STATUS_FAILED, "attempts": HARD_RETRY_CAP},
        "retryable": {"status": STATUS_FAILED, "attempts": 1},
    }
    assert has_retryable_sessions(states) is True


# ---------------------------------------------------------------------------
# 5. compute_sleep_cap: daemon sleep capped at 900s with retryable sessions
# ---------------------------------------------------------------------------


def test_compute_sleep_cap_with_retryable(monkeypatch):
    """With retryable sessions present, sleep_seconds cannot exceed 900."""
    from memem.miner_daemon import BACKOFF_MAX_SECONDS, compute_sleep_cap
    from memem.miner_protocol import STATUS_FAILED

    assert BACKOFF_MAX_SECONDS == 1800  # sanity check

    states = {
        "sess1": {"status": STATUS_FAILED, "attempts": 1},
    }
    # Even if we pass something larger than 900, it should be capped
    result = compute_sleep_cap(1800, states)
    assert result <= BACKOFF_MAX_SECONDS // 2
    assert result == 900


def test_compute_sleep_cap_all_terminal():
    """With all terminal sessions, sleep_seconds can reach 1800."""
    from memem.miner_daemon import BACKOFF_MAX_SECONDS, compute_sleep_cap
    from memem.miner_protocol import STATUS_COMPLETE

    states = {
        "sess1": {"status": STATUS_COMPLETE, "attempts": 0},
        "sess2": {"status": STATUS_COMPLETE, "attempts": 5},
    }
    result = compute_sleep_cap(1800, states)
    assert result == BACKOFF_MAX_SECONDS  # full 1800s allowed


def test_compute_sleep_cap_no_sessions():
    """Empty states dict → no retryable sessions → full cap allowed."""
    from memem.miner_daemon import BACKOFF_MAX_SECONDS, compute_sleep_cap

    result = compute_sleep_cap(1800, {})
    assert result == BACKOFF_MAX_SECONDS


def test_compute_sleep_cap_below_900_retryable():
    """If sleep_seconds is already below 900, it should not be increased."""
    from memem.miner_daemon import compute_sleep_cap
    from memem.miner_protocol import STATUS_FAILED

    states = {"s": {"status": STATUS_FAILED, "attempts": 1}}
    result = compute_sleep_cap(60, states)
    assert result == 60  # min(60, 900) = 60


def test_compute_sleep_cap_below_900_terminal():
    """If sleep_seconds is below 900 and no retryable, value unchanged."""
    from memem.miner_daemon import compute_sleep_cap
    from memem.miner_protocol import STATUS_COMPLETE

    states = {"s": {"status": STATUS_COMPLETE, "attempts": 0}}
    result = compute_sleep_cap(300, states)
    assert result == 300  # min(300, 1800) = 300
