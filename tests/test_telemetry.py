"""Tests for telemetry sidecar."""
from datetime import UTC


def test_default_telemetry(tmp_cortex_dir):
    from memem.telemetry import _get_telemetry
    tel = _get_telemetry("nonexistent")
    assert tel["access_count"] == 0
    assert tel["last_accessed"] == ""


def test_record_access(tmp_cortex_dir):
    from memem.telemetry import _get_telemetry, _record_access
    _record_access("memid123")
    tel = _get_telemetry("memid123")
    assert tel["access_count"] == 1
    assert tel["last_accessed"] != ""


def test_multiple_accesses(tmp_cortex_dir):
    from memem.telemetry import _get_telemetry, _record_access
    for _ in range(5):
        _record_access("mem456")
    tel = _get_telemetry("mem456")
    assert tel["access_count"] == 5


def test_event_log(tmp_cortex_dir):
    from memem.models import EVENT_LOG
    from memem.telemetry import _log_event
    _log_event("save", "test-id", title="test")
    assert EVENT_LOG.exists()
    assert "save" in EVENT_LOG.read_text()


def test_session_recalls_roundtrip(tmp_cortex_dir):
    from memem.telemetry import get_session_recalls, record_session_recall
    record_session_recall("sessABCDEF123", "mem11111111")
    record_session_recall("sessABCDEF123", "mem22222222")
    record_session_recall("sessABCDEF123", "mem11111111")  # dedup
    recalls = get_session_recalls("sessABCDEF123")
    assert set(recalls) == {"mem11111", "mem22222"}


def test_session_recalls_prunes_old(tmp_cortex_dir):
    """Entries older than 30 days must be dropped on write so the file
    doesn't grow unboundedly over a year of daily use."""
    import json
    from datetime import datetime, timedelta

    from memem.telemetry import (
        _SESSION_RECALLS_FILE,
        get_session_recalls,
        record_session_recall,
    )

    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    fresh_ts = datetime.now(UTC).isoformat()
    seed = {
        "oldsession01": {"ts": old_ts, "recalls": ["oldmem001"]},
        "freshsession": {"ts": fresh_ts, "recalls": ["freshmem1"]},
    }
    _SESSION_RECALLS_FILE.write_text(json.dumps(seed))

    # Writing a new entry triggers prune
    record_session_recall("newsess00000", "newmem000")

    after = json.loads(_SESSION_RECALLS_FILE.read_text())
    assert "oldsession01" not in after, "entry older than 30 days should have been pruned"
    assert "freshsession" in after
    assert "newsess00000" in after
    # Legacy list-format callers still see the list
    assert get_session_recalls("freshsession") == ["freshmem1"]


def test_session_recalls_legacy_list_format(tmp_cortex_dir):
    """Entries written by the pre-prune version (bare list) are still
    readable — they get migrated to the new dict format on next touch."""
    import json

    from memem.telemetry import (
        _SESSION_RECALLS_FILE,
        get_session_recalls,
        record_session_recall,
    )
    _SESSION_RECALLS_FILE.write_text(json.dumps({"legacysess01": ["memX"]}))
    assert get_session_recalls("legacysess01") == ["memX"]
    record_session_recall("legacysess01", "memY")
    recalls = get_session_recalls("legacysess01")
    assert set(recalls) == {"memX", "memY"}
