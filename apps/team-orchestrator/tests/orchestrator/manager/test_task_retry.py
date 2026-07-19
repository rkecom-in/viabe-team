"""VT-557 — pure retry-ladder decision tests (no DB; deterministic backoff via injected rand)."""

from __future__ import annotations

from orchestrator.backoff import MAX_ATTEMPTS
from orchestrator.manager.task_retry import decide_retry


def test_first_stall_retries_with_base_backoff():
    # rand=0.5 → zero jitter → the exact base delay (1s).
    d = decide_retry(0, 5, rand=lambda: 0.5)
    assert d.kind == "retry"
    assert d.next_attempt == 1
    assert d.delay_s == 1.0


def test_backoff_curve_is_exponential():
    # rand=0.5 → no jitter → exact 1/2/4/8 curve as attempt climbs.
    assert decide_retry(0, 5, rand=lambda: 0.5).delay_s == 1.0
    assert decide_retry(1, 5, rand=lambda: 0.5).delay_s == 2.0
    assert decide_retry(2, 5, rand=lambda: 0.5).delay_s == 4.0
    assert decide_retry(3, 5, rand=lambda: 0.5).delay_s == 8.0


def test_exhaustion_dead_letters():
    d = decide_retry(4, 5)
    assert d.kind == "dead_letter"
    assert d.next_attempt == 5
    assert d.delay_s is None


def test_jitter_stays_within_bounds():
    lo = decide_retry(0, 5, rand=lambda: 0.0).delay_s  # -25%
    hi = decide_retry(0, 5, rand=lambda: 1.0 - 1e-9).delay_s  # +25%
    assert abs(lo - 0.75) < 1e-6
    assert hi <= 1.25


def test_max_attempts_above_curve_clamps_without_overflow():
    # a budget larger than the backoff curve must not overflow compute_delay's range.
    d = decide_retry(MAX_ATTEMPTS, MAX_ATTEMPTS + 5, rand=lambda: 0.5)
    assert d.kind == "retry"
    assert d.delay_s == 16.0  # clamped to the curve cap (base[4])
