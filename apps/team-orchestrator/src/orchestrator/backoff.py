"""Jittered exponential backoff + per-vendor/per-env circuit breaker (VT-29).

Backoff
-------
``compute_delay(attempt)`` returns the wall-clock seconds before retry
``attempt`` (1-indexed). Curve: 1, 2, 4, 8, 16 seconds, with ±25%
uniform jitter. The 5-attempt cap is enforced by ``MAX_ATTEMPTS``; the
caller checks ``attempt > MAX_ATTEMPTS`` and escalates instead of
calling ``compute_delay`` again.

Circuit breaker
---------------
``CircuitBreaker`` is keyed on ``(vendor, env)`` — Twilio in staging is a
separate circuit from Twilio in prod. Counts errors in a 60-second
rolling window; opens at 10 errors, stays open for 5 minutes, then
half-closes (the next call probes — success closes the circuit, failure
re-opens for another 5 minutes).

Both pieces are pure-Python and clock-driven; tests inject a fake
``time_fn`` rather than monkeypatching ``time.time``.
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock

MAX_ATTEMPTS = 5
_BASE_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
_JITTER_FRACTION = 0.25

# Circuit-breaker policy (VT-29).
_BREAKER_THRESHOLD = 10
_BREAKER_WINDOW_S = 60.0
_BREAKER_OPEN_S = 300.0  # 5 minutes


def compute_delay(
    attempt: int,
    *,
    rand: Callable[[], float] = random.random,
) -> float:
    """Return the delay in seconds before the given retry attempt.

    ``attempt`` is 1-indexed (first retry == 1). Raises ``ValueError`` for
    ``attempt <= 0`` or ``attempt > MAX_ATTEMPTS`` — the caller MUST
    treat ``attempt > MAX_ATTEMPTS`` as "stop retrying" and escalate
    instead of calling here.

    ``rand`` defaults to ``random.random`` (returns [0.0, 1.0)). Tests
    inject a deterministic source.
    """
    if attempt <= 0:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if attempt > MAX_ATTEMPTS:
        raise ValueError(
            f"attempt {attempt} exceeds MAX_ATTEMPTS={MAX_ATTEMPTS}; escalate"
        )
    base = _BASE_DELAYS[attempt - 1]
    # Uniform jitter in [-25%, +25%].
    jitter = (rand() * 2.0 - 1.0) * _JITTER_FRACTION
    return base * (1.0 + jitter)


class BreakerState(str, Enum):
    """Breaker state. ``HALF_OPEN`` is the brief recovery probe window."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _CircuitState:
    """Per-(vendor, env) state. Internal — exposed read-only via methods."""

    errors: deque[float] = field(default_factory=deque)
    opened_at: float | None = None
    state: BreakerState = BreakerState.CLOSED


class CircuitBreaker:
    """Per-(vendor, env) rolling-window circuit breaker.

    Thread-safe via a single coarse lock — circuit transitions are rare
    relative to the request rate, so contention is negligible.
    """

    def __init__(self, *, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._time_fn = time_fn
        self._lock = Lock()
        self._circuits: dict[tuple[str, str], _CircuitState] = {}

    def allow(self, vendor: str, env: str) -> bool:
        """True if a request to (vendor, env) should be attempted."""
        with self._lock:
            circuit = self._circuits.setdefault((vendor, env), _CircuitState())
            now = self._time_fn()
            self._tick(circuit, now)
            return circuit.state != BreakerState.OPEN

    def record_success(self, vendor: str, env: str) -> None:
        """Mark a successful call. Closes a HALF_OPEN circuit."""
        with self._lock:
            circuit = self._circuits.setdefault((vendor, env), _CircuitState())
            if circuit.state == BreakerState.HALF_OPEN:
                circuit.state = BreakerState.CLOSED
                circuit.errors.clear()
                circuit.opened_at = None

    def record_failure(self, vendor: str, env: str) -> None:
        """Mark a failure. Trips the breaker once threshold/window is met."""
        with self._lock:
            circuit = self._circuits.setdefault((vendor, env), _CircuitState())
            now = self._time_fn()
            circuit.errors.append(now)
            self._tick(circuit, now)
            if circuit.state == BreakerState.HALF_OPEN:
                # Probe failed — re-open for another full open window.
                circuit.state = BreakerState.OPEN
                circuit.opened_at = now
                return
            if (
                circuit.state == BreakerState.CLOSED
                and len(circuit.errors) >= _BREAKER_THRESHOLD
            ):
                circuit.state = BreakerState.OPEN
                circuit.opened_at = now

    def state(self, vendor: str, env: str) -> BreakerState:
        """Current breaker state for (vendor, env). For tests/observability."""
        with self._lock:
            circuit = self._circuits.setdefault((vendor, env), _CircuitState())
            self._tick(circuit, self._time_fn())
            return circuit.state

    def _tick(self, circuit: _CircuitState, now: float) -> None:
        """Evict errors outside the rolling window; transition OPEN→HALF_OPEN
        once the 5-minute open window has elapsed."""
        cutoff = now - _BREAKER_WINDOW_S
        while circuit.errors and circuit.errors[0] < cutoff:
            circuit.errors.popleft()
        if (
            circuit.state == BreakerState.OPEN
            and circuit.opened_at is not None
            and now - circuit.opened_at >= _BREAKER_OPEN_S
        ):
            circuit.state = BreakerState.HALF_OPEN


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "MAX_ATTEMPTS",
    "compute_delay",
]
