"""Depth tracker (VT-35).

Counts think→tool→think cycles. Each tool dispatch followed by a
further reasoning turn increments the depth counter:

    turn 1 (think) → tool dispatch  → depth 1
    turn 2 (think) → tool dispatch  → depth 2
    ...
    turn 9 (think) → tool dispatch  → depth 9 > LIMIT(8) → cancel

The pattern is "tool dispatch occurred, then another reasoning turn
happened" — the depth increment fires at the START of the post-tool
turn (``record_reasoning_turn`` called once per ``_run_one_turn`` after
a prior ``record_tool_dispatch``).

Per-invocation reset: each run starts at depth 0.
"""

from __future__ import annotations

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.failures import HardLimitAxis

# 8 levels of nesting. Type-3 commitment — VT-35 brief. CI guard in
# .github/workflows/ci.yml enforces this literal.
DEPTH_HARD_LIMIT = 8


class DepthTracker:
    """>8 think→tool→think cycles in one run → cancel(depth)."""

    def __init__(self, ctx: CancellationContext) -> None:
        self.ctx = ctx
        self.depth = 0
        self.limit = DEPTH_HARD_LIMIT
        self._just_dispatched_tool = False

    def record_tool_dispatch(self) -> None:
        """A tool was dispatched. The NEXT reasoning turn will be a
        depth increment (a think after a tool = one new level)."""
        self._just_dispatched_tool = True

    def record_reasoning_turn(self) -> None:
        """A reasoning turn happened. If the previous beat was a tool
        dispatch, this is the post-tool think — increment depth."""
        if self._just_dispatched_tool:
            self.depth += 1
            self._just_dispatched_tool = False
            if self.depth > self.limit and not self.ctx.is_cancelled:
                self.ctx.signal(
                    HardLimitAxis.DEPTH,
                    f"depth budget exceeded: {self.depth} > {self.limit}",
                )


__all__ = ["DEPTH_HARD_LIMIT", "DepthTracker"]
