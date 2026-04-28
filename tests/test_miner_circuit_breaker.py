"""Tests for the hand-rolled CircuitBreaker in memem/miner_circuit_breaker.py."""

import pytest

from memem.miner_circuit_breaker import (
    STATE_CLOSED,
    STATE_OPEN,
    CircuitBreaker,
)
from memem.miner_errors import PermanentError, TransientError


def _make_breaker(threshold=5, duration=300.0, start_time=0.0):
    """Helper: create a CircuitBreaker with an injectable fake clock."""
    clock_value = [start_time]

    def clock():
        return clock_value[0]

    breaker = CircuitBreaker(
        failure_threshold=threshold,
        open_duration_seconds=duration,
        clock=clock,
    )
    return breaker, clock_value


class TestBreakerStartsClosed:
    def test_breaker_starts_closed(self):
        breaker, _ = _make_breaker()
        assert breaker.is_open() is False


class TestBreakerOpensOnPermanentFailures:
    def test_breaker_opens_after_threshold_permanent_failures(self):
        breaker, _ = _make_breaker(threshold=5)
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        assert breaker.is_open() is True

    def test_breaker_does_not_open_before_threshold(self):
        breaker, _ = _make_breaker(threshold=5)
        for _ in range(4):
            breaker.record_failure(PermanentError("auth broken"))
        assert breaker.is_open() is False


class TestBreakerIgnoresTransientErrors:
    def test_breaker_does_not_open_on_transient_failures(self):
        breaker, _ = _make_breaker(threshold=5)
        for _ in range(10):
            breaker.record_failure(TransientError("network blip"))
        assert breaker.is_open() is False

    def test_transient_does_not_increment_failure_count(self):
        breaker, _ = _make_breaker(threshold=5)
        for _ in range(10):
            breaker.record_failure(TransientError("blip"))
        info = breaker.state_info()
        assert info["consecutive_failures"] == 0


class TestBreakerOpenDuration:
    def test_breaker_open_rejects_until_duration_elapses(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        # Open the breaker
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        assert breaker.is_open() is True

        # Still open at 4 minutes
        clock_value[0] = 240.0
        assert breaker.is_open() is True

        # Transitions to HALF_OPEN at or past 5 minutes
        clock_value[0] = 300.0
        assert breaker.is_open() is False  # HALF_OPEN allows call through

    def test_breaker_still_open_one_second_before_duration(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        clock_value[0] = 299.0
        assert breaker.is_open() is True


class TestBreakerHalfOpenTransitions:
    def _open_and_advance(self, breaker, clock_value):
        """Helper: open the breaker and advance clock past open_duration."""
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        clock_value[0] = 300.0  # past open_duration
        # Verify it's in HALF_OPEN (is_open() returns False)
        assert breaker.is_open() is False
        return breaker

    def test_breaker_half_open_to_closed_on_success(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        self._open_and_advance(breaker, clock_value)
        # One test call came through; it succeeded
        breaker.record_success()
        info = breaker.state_info()
        assert info["state"] == STATE_CLOSED
        assert breaker.is_open() is False

    def test_breaker_half_open_to_open_on_failure(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        self._open_and_advance(breaker, clock_value)
        # One test call came through; it failed again
        breaker.record_failure(PermanentError("still broken"))
        info = breaker.state_info()
        assert info["state"] == STATE_OPEN
        # counter should be at least 1 (incremented from HALF_OPEN failure)
        assert info["consecutive_failures"] >= 1


class TestStateInfo:
    def test_state_info_returns_expected_keys_when_closed(self):
        breaker, _ = _make_breaker()
        info = breaker.state_info()
        assert "state" in info
        assert "consecutive_failures" in info
        assert "failure_threshold" in info
        # No opened_at or seconds_until_half_open when CLOSED
        assert "opened_at" not in info
        assert "seconds_until_half_open" not in info

    def test_state_info_returns_expected_keys_when_open(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        clock_value[0] = 60.0
        info = breaker.state_info()
        assert info["state"] == STATE_OPEN
        assert "opened_at" in info
        assert "seconds_until_half_open" in info
        # 300 - 60 = 240 seconds remaining
        assert info["seconds_until_half_open"] == pytest.approx(240.0)

    def test_seconds_until_half_open_is_zero_when_past_duration(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        clock_value[0] = 400.0
        # Calling is_open() will transition to HALF_OPEN, so use state_info directly
        # before the transition to check the floor behavior
        info = breaker.state_info()
        assert info["seconds_until_half_open"] == 0.0


class TestRecordSuccessResets:
    def test_record_success_resets_failure_count(self):
        breaker, _ = _make_breaker(threshold=5)
        for _ in range(3):
            breaker.record_failure(PermanentError("partial failures"))
        breaker.record_success()
        info = breaker.state_info()
        assert info["consecutive_failures"] == 0
        assert info["state"] == STATE_CLOSED

    def test_record_success_clears_opened_at(self):
        breaker, clock_value = _make_breaker(threshold=5, duration=300.0, start_time=0.0)
        for _ in range(5):
            breaker.record_failure(PermanentError("auth broken"))
        clock_value[0] = 300.0
        breaker.is_open()  # transitions to HALF_OPEN
        breaker.record_success()
        info = breaker.state_info()
        assert "opened_at" not in info
