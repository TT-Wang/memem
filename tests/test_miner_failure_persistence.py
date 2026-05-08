"""Tests for m5: persisting per-session failure counter to disk on each failure.
Also covers m9: seeding failure_counts from disk on daemon restart so the cap
enforcement picks up where the previous run left off.

Covers (m5):
1. attempts field persisted on first failure (STATUS_RETRYING)
2. attempts increments across multiple failures
3. attempts survives daemon restart (round-trip through _parse_state_line)
4. attempts defaults to 0 when not specified
5. STATUS_RETRYING is not terminal (session stays in queue)
6. STATUS_FAILED is still terminal (sanity check)

Covers (m9):
7. failure_counts seeded from disk on daemon restart (STATUS_RETRYING with attempts=2)
8. COMPLETE sessions excluded from seed (should not inflate failure counter)
9. Sessions with attempts=0 excluded from seed (no failures yet)
10. Cap enforcement trips at MAX after seeded count plus one more failure
"""

import importlib
from pathlib import Path

import pytest

from memem.miner_protocol import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RETRYING,
)


def _write_session_jsonl(state_dir: Path, name: str = "abc123", content: str = "x") -> Path:
    """Create a fake session JSONL file the miner would consider real."""
    sessions_dir = state_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    path = sessions_dir / f"{name}.jsonl"
    path.write_text(content)
    return path


@pytest.fixture(autouse=True)
def reload_session_state(tmp_cortex_dir):
    """Reload session_state after tmp_cortex_dir sets MEMEM_DIR so that
    MINED_SESSIONS_FILE points to the isolated tmp directory."""
    from memem import session_state
    importlib.reload(session_state)
    return session_state


def _ss():
    from memem import session_state
    return session_state


def test_attempts_field_persisted_on_first_failure(tmp_cortex_dir):
    """Simulate one failure: write STATUS_RETRYING with attempts=1,
    load state from disk, assert attempts == 1."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess001")

    ss.update_session_state(jsonl, STATUS_RETRYING, message="attempt 1", attempts=1)

    states = ss.load_mined_session_state()
    assert "sess001" in states
    assert states["sess001"]["attempts"] == 1
    assert states["sess001"]["status"] == STATUS_RETRYING


def test_attempts_field_increments_across_failures(tmp_cortex_dir):
    """3 failures: assert attempts goes 1, 2, 3 on successive writes."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess002")

    for count in range(1, 4):
        ss.update_session_state(
            jsonl, STATUS_RETRYING, message=f"attempt {count}", attempts=count
        )
        states = ss.load_mined_session_state()
        assert states["sess002"]["attempts"] == count


def test_attempts_survives_daemon_restart(tmp_cortex_dir):
    """Write a state with attempts=2, reload via load_mined_session_state,
    assert the loaded dict has attempts == 2.

    This is the regression-against-whitelist-drop test. If _parse_state_line
    does not include 'attempts' in its returned dict, this test will fail with
    attempts == 0 (or KeyError) even though update_session_state wrote attempts=2
    to disk. The test proves the round-trip is intact.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess003")

    ss.update_session_state(jsonl, STATUS_RETRYING, message="attempt 2", attempts=2)

    # Simulate daemon restart: reload the module so MINED_SESSIONS_FILE is
    # re-evaluated from the env (still pointing at tmp_cortex_dir), then load.
    importlib.reload(ss)
    states = ss.load_mined_session_state()

    assert "sess003" in states
    assert states["sess003"]["attempts"] == 2


def test_attempts_field_zero_default(tmp_cortex_dir):
    """update_session_state without attempts argument writes attempts=0."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess004")

    ss.update_session_state(jsonl, STATUS_COMPLETE)

    states = ss.load_mined_session_state()
    assert "sess004" in states
    assert states["sess004"]["attempts"] == 0


def test_status_retrying_is_not_terminal(tmp_cortex_dir):
    """A session with STATUS_RETRYING must NOT be terminal.

    session_is_terminal only treats COMPLETE and FAILED as terminal. STATUS_RETRYING
    must pass through so the session stays in the queue and continues to be retried.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess005")

    state = ss.update_session_state(
        jsonl, STATUS_RETRYING, message="transient failure", attempts=1
    )

    assert ss.session_is_terminal(jsonl, state) is False


def test_status_failed_is_terminal(tmp_cortex_dir):
    """Sanity check: the existing terminal logic still works for STATUS_FAILED.

    This ensures m5 changes to session_state.py did not accidentally break
    the STATUS_FAILED terminal detection that prevents retry storms.
    """
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="sess006")

    state = ss.update_session_state(
        jsonl, STATUS_FAILED, message="gave up", attempts=3
    )

    assert ss.session_is_terminal(jsonl, state) is True


# ---------------------------------------------------------------------------
# m9 tests: seed failure_counts from disk so daemon restarts pick up the count
# ---------------------------------------------------------------------------


def test_failure_counts_seeded_from_disk_on_loop_start(tmp_cortex_dir):
    """Write attempts=2/STATUS_RETRYING to disk; verify the seed helper returns
    failure_counts[session_id] == 2 so a daemon restart doesn't reset the cap.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest01")
    ss.update_session_state(jsonl, STATUS_RETRYING, message="transient", attempts=2)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert seeded.get("seedtest01") == 2


def test_failure_counts_skip_completed_sessions(tmp_cortex_dir):
    """COMPLETE sessions must NOT appear in the seeded failure_counts.

    A session that succeeded and later re-enters the queue (after JSONL change)
    should start with a fresh counter, not carry forward stale failure counts.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest02")
    # Write attempts=2 but status=COMPLETE (e.g. succeeded on 3rd try,
    # attempts was set to the prior retrying count).
    ss.update_session_state(jsonl, STATUS_COMPLETE, message="", attempts=2)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert "seedtest02" not in seeded


def test_failure_counts_skip_zero_attempts(tmp_cortex_dir):
    """Sessions with attempts=0 must NOT appear in the seeded failure_counts.

    A fresh session that has never failed should not appear in failure_counts;
    seeding it with 0 would be harmless but misleading. Excluding it keeps the
    dict clean and avoids accidental cap triggers at exactly MAX attempts.
    """
    from memem.miner_daemon import _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest03")
    ss.update_session_state(jsonl, STATUS_RETRYING, message="", attempts=0)

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    assert "seedtest03" not in seeded


def test_session_marked_failed_at_cap_after_restart(tmp_cortex_dir):
    """Simulate daemon restart with attempts=2 (one short of MAX=3).

    After seeding failure_counts from disk, one more failure should trigger
    the cap and produce STATUS_FAILED in persisted state.

    This is tested at the helper level (not the full loop) to avoid needing
    to mock subprocesses. The cap-enforcement branch in _run_loop uses the
    same counter dict that _seed_failure_counts_from_state populates, so
    verifying the dict value is sufficient to prove the integration.

    For the full end-to-end "terminal after re-entry" path see:
        tests/test_miner_auth_crush.py::test_session_is_terminal_reincludes_failed_session_on_content_change
    """
    from memem.miner_daemon import MAX_SESSION_FAILURES, _seed_failure_counts_from_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="seedtest04")
    # Persist attempts=MAX-1 (the state just before the cap fires)
    ss.update_session_state(
        jsonl, STATUS_RETRYING, message="attempt 2", attempts=MAX_SESSION_FAILURES - 1
    )

    states = ss.load_mined_session_state()
    seeded = _seed_failure_counts_from_state(states)

    # One more failure would make count == MAX_SESSION_FAILURES which trips the cap.
    simulated_next_count = seeded.get("seedtest04", 0) + 1
    assert simulated_next_count == MAX_SESSION_FAILURES

    # Simulate cap enforcement: write STATUS_FAILED at cap
    if simulated_next_count >= MAX_SESSION_FAILURES:
        ss.update_session_state(
            jsonl, STATUS_FAILED,
            message=f"miner gave up after {simulated_next_count} consecutive failures",
            attempts=simulated_next_count,
        )

    final_states = ss.load_mined_session_state()
    assert final_states["seedtest04"]["status"] == STATUS_FAILED
    assert final_states["seedtest04"]["attempts"] == MAX_SESSION_FAILURES


def test_fatal_api_error_persists_status_failed_before_raise(tmp_cortex_dir, tmp_vault, monkeypatch):
    """Regression: when _mine_session classifies an exception as fatal-api-error
    (e.g. 'subprocess timed out after 300s'), it MUST persist STATUS_FAILED for
    the offending session BEFORE raising FatalMinerError. Otherwise a wrapper
    restart re-picks the session, hits the same hang, and crash-loops through
    the 5-in-60s wrapper budget.

    Reproduced in production: session 9612f54c-bbd timed out on 2026-04-30,
    2026-05-01, and 2026-05-04 — the same session each time, because no
    STATUS_FAILED was written between crashes.
    """
    import importlib
    from unittest.mock import patch

    from memem import miner_daemon, session_state
    importlib.reload(session_state)
    importlib.reload(miner_daemon)

    jsonl = _write_session_jsonl(tmp_cortex_dir, name="hangsim01")

    # Force _run_server_command to raise a RetryableMinerError whose text
    # matches a fatal-api pattern ('timed out' is one such pattern).
    fake_exc = miner_daemon.RetryableMinerError(
        "subprocess timed out after 300s; killed process group"
    )
    with (
        patch.object(miner_daemon, "_run_server_command", side_effect=fake_exc),
        pytest.raises(miner_daemon.FatalMinerError),
    ):
        miner_daemon._mine_session(jsonl)

    # The session must be marked STATUS_FAILED on disk.
    states = session_state.load_mined_session_state()
    assert "hangsim01" in states, "session state was not persisted before fatal raise"
    assert states["hangsim01"]["status"] == STATUS_FAILED
    # attempts should be at MAX so a future restart with re-seeding immediately
    # treats it as DLQ-eligible.
    assert states["hangsim01"]["attempts"] >= miner_daemon.MAX_SESSION_FAILURES


# ---------------------------------------------------------------------------
# Hard retry cap (v1.2.2): a FAILED session past HARD_RETRY_CAP stays terminal
# regardless of content-fingerprint changes
# ---------------------------------------------------------------------------


def test_failed_session_stays_terminal_past_hard_retry_cap(tmp_cortex_dir):
    """A FAILED session with attempts >= HARD_RETRY_CAP and offset_bytes at the
    current file size must stay terminal — no new content past the offset means
    the hard cap protection holds.

    v1.4.0 update: the hard cap now allows re-entry only when new content has
    appeared past offset_bytes. When offset_bytes == file_size, no new content
    is available and the cap protection still applies. This test verifies that
    setting offset_bytes to the current file size correctly keeps the session
    terminal even after the file gets more content (because we update the stored
    state, i.e., we test the "no new content" scenario directly).
    """
    from memem import session_state
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="hardcap01")
    file_size = jsonl.stat().st_size

    # Mark as FAILED past the hard cap with offset_bytes == file_size
    # (meaning we've already read all current content — no new content available)
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="exceeded retry budget",
        attempts=session_state.HARD_RETRY_CAP + 10,
        offset_bytes=file_size,
    )

    states = ss.load_mined_session_state()
    # offset_bytes == file_size → no new content → must be terminal
    assert ss.session_is_terminal(jsonl, states["hardcap01"]) is True

    # Grow the file — now file_size > offset_bytes (new content past offset)
    # Under v1.4.0 semantics, this allows re-entry so incremental mining
    # can process just the new delta. This is the intended new behavior.
    jsonl.write_text(jsonl.read_text() + "\n" + "x" * 5000)
    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["hardcap01"]) is False, (
        "FAILED session past HARD_RETRY_CAP must re-enter queue when new content "
        "appears past offset_bytes — incremental mining can safely process the delta"
    )


def test_failed_session_below_hard_cap_still_re_includes_on_change(tmp_cortex_dir):
    """Sessions below the hard cap must still re-enter the queue when content
    changes — preserving the 'transient failure can recover' design.
    """
    from memem import session_state
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="softcap01")

    # FAILED with attempts well below the hard cap
    assert session_state.HARD_RETRY_CAP >= 2
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="transient",
        attempts=1,
    )

    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["softcap01"]) is True

    # Mutate the JSONL — should re-enter the queue
    jsonl.write_text(jsonl.read_text() + "\n" + "y" * 5000)
    states = ss.load_mined_session_state()
    assert ss.session_is_terminal(jsonl, states["softcap01"]) is False, (
        "FAILED session below HARD_RETRY_CAP must re-enter queue on content change"
    )


def test_settle_seconds_default_is_30_minutes(tmp_cortex_dir, monkeypatch):
    """SETTLE_SECONDS default bumped 300s → 1800s.

    Previous default was 5 min, which was too short for actively-typed-into
    Claude Code sessions: the miner picked them up between user turns and the
    Haiku subprocess timed out on a still-growing file. 30 min ensures the
    user has stopped typing for a meaningful gap before mining is attempted.
    """
    import importlib

    monkeypatch.delenv("MEMEM_MINER_SETTLE_SECONDS", raising=False)
    monkeypatch.delenv("CORTEX_MINER_SETTLE_SECONDS", raising=False)
    from memem import session_state
    importlib.reload(session_state)
    assert session_state.SETTLE_SECONDS == 1800, (
        f"settle window should default to 1800s (30 min); got {session_state.SETTLE_SECONDS}"
    )


# ---------------------------------------------------------------------------
# m2 tests: incremental session mining with byte offset (v1.4.0)
# ---------------------------------------------------------------------------


def _write_session_jsonl_sized(state_dir, name: str, size_bytes: int) -> "Path":
    """Create a fake session JSONL of at least ``size_bytes`` bytes with parseable messages."""
    import json as _json
    sessions_dir = state_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    path = sessions_dir / f"{name}.jsonl"
    # Write enough valid user+assistant messages to exceed size_bytes
    lines = []
    # Each message is ~80 bytes; generate enough to cover size_bytes
    msg_size = 80
    count = (size_bytes // msg_size) + 10
    for i in range(count):
        if i % 2 == 0:
            lines.append(_json.dumps({"type": "user", "message": {"content": f"question {i} " + "q" * 40}}))
        else:
            lines.append(_json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"answer {i} " + "a" * 40}]}}))
    content = "\n".join(lines) + "\n"
    path.write_text(content)
    return path


def test_offset_bytes_persisted_on_first_mine(tmp_cortex_dir, tmp_vault, monkeypatch):
    """First successful mining of a ~50KB JSONL sets offset_bytes to the file size in DB."""
    import importlib
    from unittest.mock import patch

    from memem import mining, session_state
    importlib.reload(session_state)
    importlib.reload(mining)

    jsonl = _write_session_jsonl_sized(tmp_cortex_dir, "offsettest01", 50_000)
    expected_size = jsonl.stat().st_size

    # Mock Haiku to return an empty insights list (simulates "nothing worth saving")
    with patch.object(mining, "_summarize_session_haiku", return_value=[]):
        mining.mine_session(str(jsonl))

    states = session_state.load_mined_session_state()
    assert "offsettest01" in states
    # offset_bytes should be set to file size (we read to EOF from offset 0)
    assert states["offsettest01"]["offset_bytes"] == expected_size


def test_subsequent_mining_reads_only_delta(tmp_cortex_dir, tmp_vault, monkeypatch):
    """File grows from 50KB to 80KB; second mine reads only the trailing 30KB (seek check)."""
    import importlib
    from unittest.mock import patch

    from memem import mining, session_state
    importlib.reload(session_state)
    importlib.reload(mining)

    jsonl = _write_session_jsonl_sized(tmp_cortex_dir, "offsettest02", 50_000)
    file_size_after_first = jsonl.stat().st_size

    # First mine: set offset to file size
    with patch.object(mining, "_summarize_session_haiku", return_value=[]):
        mining.mine_session(str(jsonl))

    states = session_state.load_mined_session_state()
    assert states["offsettest02"]["offset_bytes"] == file_size_after_first

    # Grow the file to ~80KB
    with open(jsonl, "ab") as fh:
        fh.write(b"z" * 30_000)
    assert jsonl.stat().st_size >= 80_000

    # Second mine: mock open to spy on seek calls
    original_open = open

    opened_files = []

    def spy_open(path, mode="r", **kwargs):
        fh = original_open(path, mode, **kwargs)
        if str(path) == str(jsonl):
            opened_files.append(fh)
            original_seek = fh.seek

            def recording_seek(offset, *args):
                fh._spy_seek_args = (offset,) + args
                return original_seek(offset, *args)
            fh.seek = recording_seek
        return fh

    with (
        patch.object(mining, "_summarize_session_haiku", return_value=[]),
        patch("builtins.open", side_effect=spy_open),
    ):
        mining.mine_session(str(jsonl))

    # Verify at least one open call was made with a seek to the stored offset
    assert opened_files, "Expected file to be opened for incremental read"
    seek_offsets = [
        fh._spy_seek_args[0]
        for fh in opened_files
        if hasattr(fh, "_spy_seek_args")
    ]
    assert any(offset == file_size_after_first for offset in seek_offsets), (
        f"Expected seek to {file_size_after_first} (first mine's offset), "
        f"got seek offsets: {seek_offsets}"
    )


def test_offset_bytes_round_trip_through_db(tmp_cortex_dir):
    """Write state with offset_bytes=12345, reload from DB, assert preserved."""
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="offsetrtt01")

    ss.update_session_state(jsonl, STATUS_COMPLETE, message="done", attempts=0, offset_bytes=12345)

    states = ss.load_mined_session_state()
    assert "offsetrtt01" in states
    assert states["offsetrtt01"]["offset_bytes"] == 12345


def test_offset_bytes_round_trip_through_jsonl_fallback(tmp_cortex_dir):
    """_parse_state_line must include offset_bytes to avoid the whitelist-drop bug.

    If offset_bytes is added to the DB but not to _parse_state_line, the JSONL
    migration path (used on daemon restart from a legacy file) silently drops
    the field, making offset tracking dead across daemon restart for any session
    that was migrated from JSONL format.
    """
    import json

    from memem.session_state import _parse_state_line

    raw = json.dumps({
        "session_id": "offsetrtt02",
        "status": STATUS_COMPLETE,
        "mtime_ns": 1_000_000,
        "size": 100,
        "version": "2",
        "updated_at": "2026-01-01T00:00:00Z",
        "message": "",
        "attempts": 0,
        "offset_bytes": 99999,
    })

    result = _parse_state_line(raw)
    assert result is not None
    assert result.get("offset_bytes") == 99999, (
        "offset_bytes missing from _parse_state_line output — "
        "this is the whitelist-drop bug: field written to disk but not read back"
    )


def test_partial_failure_leaves_offset_unchanged(tmp_cortex_dir, tmp_vault, monkeypatch):
    """Set offset=5000, simulate Haiku timeout; assert offset still 5000 in state."""
    import importlib
    import json as _json

    from memem import mining, session_state
    importlib.reload(session_state)
    importlib.reload(mining)

    from unittest.mock import patch

    ss = _ss()

    # Write a JSONL with user messages filling the first 5000 bytes (the "already mined" part)
    sessions_dir = tmp_cortex_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    jsonl = sessions_dir / "offsetfail01.jsonl"

    # Write initial content (first ~5000 bytes)
    first_part = ""
    while len(first_part.encode()) < 5000:
        first_part += _json.dumps({"type": "user", "message": {"content": "old question " + "q" * 40}}) + "\n"
    jsonl.write_text(first_part)
    initial_size = jsonl.stat().st_size

    # Pre-seed state at initial_size (we "already mined" everything up to here)
    ss.update_session_state(jsonl, STATUS_COMPLETE, message="first mine", attempts=0, offset_bytes=initial_size)

    # Append new content to the file (simulating session growth)
    new_part = ""
    while len(new_part.encode()) < 2000:
        new_part += _json.dumps({"type": "user", "message": {"content": "new question " + "n" * 40}}) + "\n"
        new_part += _json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "new answer " + "a" * 40}]}}) + "\n"
    with open(jsonl, "a") as fh:
        fh.write(new_part)

    # Now simulate Haiku failure on second mine (the delta read)
    with patch.object(mining, "_summarize_session_haiku",
                      side_effect=mining.TransientMiningError("timed out")):
        try:
            mining.mine_session(str(jsonl))
        except mining.TransientMiningError:
            pass

    states = ss.load_mined_session_state()
    assert "offsetfail01" in states
    assert states["offsetfail01"]["offset_bytes"] == initial_size, (
        f"offset_bytes must remain unchanged (={initial_size}) after a failure so the next "
        f"attempt re-tries the same delta; got {states['offsetfail01']['offset_bytes']}"
    )


def test_hard_capped_session_re_enters_on_new_content(tmp_cortex_dir):
    """Hard-capped FAILED session re-enters queue when file_size > stored offset_bytes."""
    from memem import session_state
    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="hcap_reenter01")

    # Mark as FAILED past HARD_RETRY_CAP with offset_bytes=5000
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="exceeded retry budget",
        attempts=session_state.HARD_RETRY_CAP + 10,
        offset_bytes=5000,
    )

    # Grow the file so file_size > stored offset (8000 > 5000)
    # The current file is small (from _write_session_jsonl which writes a tiny file)
    # We need to grow it to at least 8000 bytes
    with open(jsonl, "ab") as fh:
        fh.write(b"n" * 8000)

    states = ss.load_mined_session_state()
    # file_size > offset_bytes → should NOT be terminal (new content available)
    assert ss.session_is_terminal(jsonl, states["hcap_reenter01"]) is False, (
        "Hard-capped FAILED session must re-enter queue when file_size > offset_bytes; "
        "new content is available past the stored offset"
    )


def test_hard_capped_session_stays_terminal_when_no_new_content(tmp_cortex_dir):
    """Hard-capped FAILED session stays terminal when file_size == stored offset_bytes."""
    from memem import session_state
    ss = _ss()

    # Write a file of a known size
    sessions_dir = tmp_cortex_dir.parent / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    jsonl = sessions_dir / "hcap_terminal01.jsonl"
    content = b"x" * 5000
    jsonl.write_bytes(content)
    file_size = jsonl.stat().st_size  # should be 5000

    # Mark as FAILED past HARD_RETRY_CAP with offset_bytes == file_size
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="exceeded retry budget",
        attempts=session_state.HARD_RETRY_CAP + 10,
        offset_bytes=file_size,
    )

    states = ss.load_mined_session_state()
    # file_size == offset_bytes → no new content → must stay terminal
    assert ss.session_is_terminal(jsonl, states["hcap_terminal01"]) is True, (
        "Hard-capped FAILED session must stay terminal when file_size == offset_bytes; "
        "no new content past the stored offset, original protection holds"
    )


def test_pre_v1_4_capped_session_with_offset_zero_stays_terminal(tmp_cortex_dir):
    """Migration regression guard.

    Before v1.4.0, capped sessions in the DB have offset_bytes=0 (ALTER TABLE
    default). After v1.4.0's session_is_terminal change, the cap-exception
    check would compare fingerprint.size against offset_bytes — which would
    always be > 0, re-entering EVERY pre-existing capped session into an
    infinite retry loop (the very bug HARD_RETRY_CAP was added to prevent).

    Fix: when offset_bytes==0 but state.size is non-zero, treat the offset
    as size (equivalent to "fully mined through pre-cap fingerprint size").
    """
    from memem import session_state

    ss = _ss()
    jsonl = _write_session_jsonl(tmp_cortex_dir, name="migrated01", content="x" * 5000)

    # Simulate a pre-v1.4.0 capped row: high attempts, FAILED status, fingerprint
    # captured at cap time, but offset_bytes=0 (the ALTER TABLE default).
    ss.update_session_state(
        jsonl, STATUS_FAILED,
        message="capped pre-v1.4.0",
        attempts=session_state.HARD_RETRY_CAP + 5,
        # offset_bytes intentionally NOT passed → defaults to 0
    )
    states = ss.load_mined_session_state()
    state = states["migrated01"]
    assert state.get("offset_bytes", 0) == 0, "test setup: offset must be 0 (migration default)"
    assert int(state.get("size") or 0) > 0, "test setup: size must be non-zero"

    # Without the migration fallback, this would return False (re-enter).
    # With the fix: file size == stored size → no new content → still terminal.
    assert ss.session_is_terminal(jsonl, state) is True, (
        "pre-v1.4.0 capped session with offset_bytes=0 must STAY terminal "
        "when no new content has appeared (migration safety)"
    )

    # Now grow the file past the stored size — re-entry should now be allowed
    jsonl.write_text(jsonl.read_text() + "y" * 5000)
    states = ss.load_mined_session_state()
    state = states["migrated01"]
    # NOTE: load_mined_session_state may not refresh fingerprint; the migration
    # fallback uses state["size"] (stored at cap time), so fingerprint.size now > stored size.
    assert ss.session_is_terminal(jsonl, state) is False, (
        "growth past stored size must allow re-entry for incremental mine"
    )
