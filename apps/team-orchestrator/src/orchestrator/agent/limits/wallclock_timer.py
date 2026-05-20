"""Wall-clock timer (VT-35).

Sync/async resolution
---------------------
The sales_recovery agent loop is SYNCHRONOUS — it calls
``client.messages.create()`` (blocking) on the main thread. An asyncio
timer cannot interrupt a blocking sync call cleanly. Per VT-35 brief
option (b): a monotonic-clock deadline checked at each turn boundary
PLUS a per-turn HTTP timeout that bounds any single ``messages.create``
call.

This combo guards both axes of "run too long":
  - many fast turns accumulating past 300s — caught by the deadline
    check at each turn boundary
  - one hung turn taking >5min — caught by the per-turn HTTP timeout
    passed to ``messages.create(timeout=...)`` (anthropic SDK uses
    httpx.Timeout under the hood)

No asyncio task, no separate thread. Resource cleanup is trivial — the
WallclockTimer is a plain object that goes out of scope when
``run_sales_recovery_agent`` returns. No zombies possible.

Why not run blocking call in a thread + watchdog (option a)?
  - Cancelling a thread blocking on httpx requires aborting the
    underlying socket; httpx exposes this via timeouts more cleanly
    than via thread cancellation.
  - Per-turn HTTP timeout achieves the same hard ceiling without
    introducing thread-lifecycle complexity (which would itself need
    testing for zombies — exactly the failure mode option (b) avoids).

If the loop later goes async (e.g. for token-by-token streaming), this
module's interface stays the same — only the underlying mechanism
changes from "check at turn boundary" to "asyncio.wait_for".
"""

from __future__ import annotations

import time
from collections.abc import Callable

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.failures import HardLimitAxis

# 5 minutes wall-clock per run. Type-3 commitment — VT-35 brief. CI
# guard in .github/workflows/ci.yml enforces this literal.
WALL_CLOCK_HARD_LIMIT_S = 300.0

# Per-turn HTTP timeout passed to messages.create. Bounds any single
# round-trip so a hung turn cannot exceed the run-level budget all by
# itself. 60s is generous — Haiku on a 1024-token cap completes in <10s
# typically, Opus in <30s. The run-level deadline still bounds the
# total wall-clock; this is just the per-call hard ceiling.
PER_TURN_HTTP_TIMEOUT_S = 60.0


class WallclockTimer:
    """>300s wall-clock on one run → cancel(wall_clock).

    ``time_fn`` defaults to ``time.monotonic`` and is injectable for
    tests (FakeClock pattern from PR #29's CircuitBreaker).
    """

    def __init__(
        self,
        ctx: CancellationContext,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ctx = ctx
        self.time_fn = time_fn
        self.start = self.time_fn()
        self.limit_s = WALL_CLOCK_HARD_LIMIT_S
        self.deadline = self.start + self.limit_s

    def check(self) -> None:
        """If we are past the deadline, signal cancel(wall_clock)."""
        if self.time_fn() > self.deadline and not self.ctx.is_cancelled:
            elapsed = self.time_fn() - self.start
            self.ctx.signal(
                HardLimitAxis.WALL_CLOCK,
                f"wallclock budget exceeded: {elapsed:.1f}s > {self.limit_s}s",
            )


__all__ = [
    "PER_TURN_HTTP_TIMEOUT_S",
    "WALL_CLOCK_HARD_LIMIT_S",
    "WallclockTimer",
]
