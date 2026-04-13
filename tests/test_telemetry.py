"""Tests for telemetry sidecar."""



def test_default_telemetry(tmp_cortex_dir):
    from telemetry import _get_telemetry
    tel = _get_telemetry("nonexistent")
    assert tel["access_count"] == 0
    assert tel["last_accessed"] == ""


def test_record_access(tmp_cortex_dir):
    from telemetry import _get_telemetry, _record_access
    _record_access("memid123")
    tel = _get_telemetry("memid123")
    assert tel["access_count"] == 1
    assert tel["last_accessed"] != ""


def test_multiple_accesses(tmp_cortex_dir):
    from telemetry import _get_telemetry, _record_access
    for _ in range(5):
        _record_access("mem456")
    tel = _get_telemetry("mem456")
    assert tel["access_count"] == 5


def test_event_log(tmp_cortex_dir):
    from models import EVENT_LOG
    from telemetry import _log_event
    _log_event("save", "test-id", title="test")
    assert EVENT_LOG.exists()
    assert "save" in EVENT_LOG.read_text()
