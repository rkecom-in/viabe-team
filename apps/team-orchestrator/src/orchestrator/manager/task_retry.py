"""VT-557 (B6) — the deterministic task retry ladder (pure; no DB, no clock beyond the injected rand).

The stalled-task reaper (orphan_reaper.reap_stalled_manager_tasks) calls ``decide_retry`` for every
task it catches stalled. One call = one recorded stall attempt: either RETRY (bounded, with a
deterministic-backoff ``delay_s`` from backoff.compute_delay) or DEAD_LETTER once the budget is spent.

Kept pure + dep-less so it unit-tests without a database and its backoff curve is asserted exactly
(the reaper owns the DB write; this owns the decision). Reuses backoff.compute_delay — the same
1/2/4/8/16s ±25% curve the vendor-retry path uses — so there is ONE backoff policy, not two.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from orchestrator.backoff import MAX_ATTEMPTS, compute_delay

RetryKind = Literal["retry", "dead_letter"]


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """One reaper decision. ``next_attempt`` is the incremented count to persist; ``delay_s`` is the
    backoff before the task is retry-eligible again (None on dead_letter — no further retry)."""

    kind: RetryKind
    next_attempt: int
    delay_s: float | None


def decide_retry(
    attempt: int,
    max_attempts: int,
    *,
    rand: Callable[[], float] = random.random,
) -> RetryDecision:
    """Decide what to do with a task caught stalled on its ``attempt``-th prior stall.

    ``next_attempt = attempt + 1``. If it reaches ``max_attempts`` the task is DEAD_LETTER (retry
    budget spent); otherwise RETRY with ``delay_s = compute_delay(next_attempt)``. The delay is
    clamped to the backoff curve's cap (MAX_ATTEMPTS) so a ``max_attempts`` configured above the
    curve never overflows compute_delay's range.
    """
    next_attempt = attempt + 1
    if next_attempt >= max_attempts:
        return RetryDecision(kind="dead_letter", next_attempt=next_attempt, delay_s=None)
    delay = compute_delay(min(next_attempt, MAX_ATTEMPTS), rand=rand)
    return RetryDecision(kind="retry", next_attempt=next_attempt, delay_s=delay)


__all__ = ["RetryDecision", "RetryKind", "decide_retry"]
