"""Tests for now_iso() and parse_iso_dt() in memem/models.py."""
from datetime import UTC, datetime, timedelta


def test_now_iso_round_trips_to_datetime():
    """parse_iso_dt(now_iso()) returns a datetime close to now (within 1s)."""
    from memem.models import now_iso, parse_iso_dt

    ts = now_iso()
    dt = parse_iso_dt(ts)

    assert dt is not None, f"parse_iso_dt returned None for {ts!r}"
    assert dt.tzinfo is not None, "Parsed datetime must be timezone-aware"

    now = datetime.now(UTC)
    diff = abs((now - dt).total_seconds())
    assert diff < 1.0, f"Round-tripped datetime differs by {diff:.3f}s from now"


def test_parse_iso_dt_z_suffix():
    """parse_iso_dt handles the 'Z' UTC suffix."""
    from memem.models import parse_iso_dt

    dt = parse_iso_dt("2026-05-08T15:30:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 8
    assert dt.hour == 15
    assert dt.minute == 30
    assert dt.second == 0
    # Must be UTC
    assert dt.utcoffset() == timedelta(0)


def test_parse_iso_dt_explicit_utc_offset():
    """parse_iso_dt handles explicit +00:00 offset."""
    from memem.models import parse_iso_dt

    dt = parse_iso_dt("2026-05-08T15:30:00+00:00")
    assert dt is not None
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 15


def test_parse_iso_dt_positive_offset():
    """parse_iso_dt handles positive UTC offset."""
    from memem.models import parse_iso_dt

    dt = parse_iso_dt("2026-05-08T15:30:00+05:30")
    assert dt is not None
    # Should be timezone-aware
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_iso_dt_naive_string_treated_as_utc():
    """Naive datetime strings (no timezone) are treated as UTC."""
    from memem.models import parse_iso_dt

    dt = parse_iso_dt("2026-05-08T15:30:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0), "Naive datetime must be localized to UTC"


def test_parse_iso_dt_returns_none_on_empty():
    """Empty string returns None."""
    from memem.models import parse_iso_dt

    assert parse_iso_dt("") is None


def test_parse_iso_dt_returns_none_on_garbage():
    """Garbage input returns None without raising."""
    from memem.models import parse_iso_dt

    assert parse_iso_dt("not-a-timestamp") is None
    assert parse_iso_dt("2026/05/08") is None
    assert parse_iso_dt("hello world") is None


def test_now_iso_returns_string():
    """now_iso() returns a non-empty string."""
    from memem.models import now_iso

    ts = now_iso()
    assert isinstance(ts, str)
    assert len(ts) > 0
