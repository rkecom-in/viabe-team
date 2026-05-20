"""Hard-limit enforcement layer (VT-35).

Four independent enforcers + a first-wins cancel coordinator wired into
the sales_recovery agent loop. The orchestrator measures; the agent has
no visibility into its own usage (Pillar 1). Termination is unilateral
and immediate — no soft-warning thresholds, no graceful-stop.

See ``docs/team/sr-agent-skeleton.md`` for the wiring map and the
sync-vs-async wall-clock resolution.
"""

from orchestrator.agent.limits.coordinator import CancellationContext
from orchestrator.agent.limits.depth_tracker import DEPTH_HARD_LIMIT, DepthTracker
from orchestrator.agent.limits.token_meter import TokenMeter
from orchestrator.agent.limits.tool_counter import (
    TOOL_CALL_HARD_LIMIT,
    ToolCounter,
)
from orchestrator.agent.limits.wallclock_timer import (
    PER_TURN_HTTP_TIMEOUT_S,
    WALL_CLOCK_HARD_LIMIT_S,
    WallclockTimer,
)

__all__ = [
    "CancellationContext",
    "DEPTH_HARD_LIMIT",
    "DepthTracker",
    "PER_TURN_HTTP_TIMEOUT_S",
    "TOOL_CALL_HARD_LIMIT",
    "TokenMeter",
    "ToolCounter",
    "WALL_CLOCK_HARD_LIMIT_S",
    "WallclockTimer",
]
