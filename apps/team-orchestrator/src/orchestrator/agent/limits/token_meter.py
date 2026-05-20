"""Token meter (VT-35).

Reads ``Message.usage`` at the per-turn boundary (the loop is
non-streaming — VT-32 / CL-242). Accumulates input + output tokens
across every turn of one run; if the running total exceeds the
run-level hard ceiling (80K — VT-32's ``_RUN_LEVEL_TOKEN_HARD_LIMIT``)
the coordinator is signalled.

Per-turn (not mid-turn) check is sufficient: a single turn is capped by
``_MAX_OUTPUT_TOKENS_PER_TURN = 1024`` so it cannot blow the run-level
80K budget within one round-trip even with input+output combined.

Per-invocation reset: each ``run_sales_recovery_agent`` call instantiates
its own TokenMeter; budgets do NOT carry across dispatches.
"""

from __future__ import annotations

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.failures import HardLimitAxis

# Imported lazily inside record_turn to avoid pulling sales_recovery into
# the limits package's import graph (sales_recovery imports limits/);
# circular-import guard.


class TokenMeter:
    """Run-level token accumulator. >80K total → cancel(tokens)."""

    def __init__(self, ctx: CancellationContext) -> None:
        self.ctx = ctx
        self.total = 0
        # Read the hard limit from sales_recovery's existing constant
        # (VT-32) so there is ONE source of truth. Imported lazily here
        # to avoid the circular import.
        from orchestrator.agent.sales_recovery import _RUN_LEVEL_TOKEN_HARD_LIMIT

        self.limit = _RUN_LEVEL_TOKEN_HARD_LIMIT

    def record_turn(self, *, input_tokens: int, output_tokens: int) -> None:
        """Add one turn's usage. Signals cancel if cumulative > limit."""
        self.total += input_tokens + output_tokens
        if self.total > self.limit and not self.ctx.is_cancelled:
            self.ctx.signal(
                HardLimitAxis.TOKENS,
                f"token budget exceeded: {self.total} > {self.limit}",
            )


__all__ = ["TokenMeter"]
