"""Tool counter (VT-35).

Wraps the ``_dispatch_tool`` seam. Increments on every tool dispatch
attempt — successful AND failed (a tool that errored still consumed
budget, both wall-clock and LLM follow-up reasoning).

Strict ``> LIMIT`` semantics: ``LIMIT == 25`` means exactly 25 tool
calls are permitted; the 26th increment trips cancellation. The test
suite locks this boundary explicitly.

Per-invocation reset: each run starts at zero.
"""

from __future__ import annotations

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.failures import HardLimitAxis

# 25 tool calls per run. Type-3 commitment — VT-35 brief. CI guard in
# .github/workflows/ci.yml enforces this literal so a future change to
# the value fails the build.
TOOL_CALL_HARD_LIMIT = 25


class ToolCounter:
    """>25 tool calls in one run → cancel(tool_calls)."""

    def __init__(self, ctx: CancellationContext) -> None:
        self.ctx = ctx
        self.count = 0
        self.limit = TOOL_CALL_HARD_LIMIT

    def record_dispatch(self) -> None:
        """Increment the counter; signal cancel if > limit."""
        self.count += 1
        if self.count > self.limit and not self.ctx.is_cancelled:
            self.ctx.signal(
                HardLimitAxis.TOOL_CALLS,
                f"tool call budget exceeded: {self.count} > {self.limit}",
            )


__all__ = ["TOOL_CALL_HARD_LIMIT", "ToolCounter"]
