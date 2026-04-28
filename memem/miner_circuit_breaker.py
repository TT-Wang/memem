"""Circuit breaker for the LLM subprocess.

States: CLOSED -> OPEN (after N consecutive PermanentError) -> HALF_OPEN
(after timeout) -> CLOSED on success or back to OPEN on failure.

When OPEN, the daemon keeps polling but instant-fails new items into
STATUS_BLOCKED instead of spawning subprocesses. This is the 'stop
digging' pattern — if the LLM CLI is missing/auth-broken, retrying
each session individually wastes time and floods logs; better to
short-circuit and let the user fix the underlying issue.

Hand-rolled (no pybreaker dep) — the surface area is small and the
state machine is tiny.
"""

import time

from memem.miner_errors import PermanentError

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

DEFAULT_FAILURE_THRESHOLD = 5      # consecutive PermanentErrors to open
DEFAULT_OPEN_DURATION = 300        # seconds in OPEN before HALF_OPEN test


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        open_duration_seconds: float = DEFAULT_OPEN_DURATION,
        clock=time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.open_duration_seconds = open_duration_seconds
        self._clock = clock
        self._state = STATE_CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def record_success(self) -> None:
        """Reset failure counter and close breaker."""
        self._consecutive_failures = 0
        self._state = STATE_CLOSED
        self._opened_at = None

    def record_failure(self, exc: BaseException) -> None:
        """Increment failure counter; only PermanentError counts toward opening.

        Why: TransientError is, by definition, expected to pass on retry.
        Counting it would cause flaps to open the breaker on noise.
        """
        if not isinstance(exc, PermanentError):
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._state = STATE_OPEN
            self._opened_at = self._clock()

    def is_open(self) -> bool:
        """True if the breaker rejects new work.

        Transitions OPEN -> HALF_OPEN if the open_duration has elapsed,
        and HALF_OPEN allows the next single call through (caller's
        responsibility to call record_success/record_failure on the result).
        """
        if self._state == STATE_CLOSED:
            return False
        if self._state == STATE_HALF_OPEN:
            return False  # caller is allowed one test call
        # OPEN
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self.open_duration_seconds:
            self._state = STATE_HALF_OPEN
            return False
        return True

    def state_info(self) -> dict:
        """Return dict for --status display (m16 will use this)."""
        info = {
            "state": self._state,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
        }
        if self._opened_at is not None:
            info["opened_at"] = self._opened_at
            elapsed = self._clock() - self._opened_at
            info["seconds_until_half_open"] = max(0, self.open_duration_seconds - elapsed)
        return info
