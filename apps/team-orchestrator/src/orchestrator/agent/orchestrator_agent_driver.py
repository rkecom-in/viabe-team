"""VT-125 OrchestratorAgentDriver — hard-limit enforcement for orchestrator-agent.

Wraps the langchain `create_agent` runnable returned by
`build_orchestrator_agent` so each invocation tracks:

- **Tool calls:** 5 per invocation
- **Cumulative tokens (input + output):** 10,000
- **Wall clock:** 120 seconds
- **Cost:** ₹5 (500 paise)
- **Depth (specialist spawn nesting):** 3 — caller-supplied; driver enforces

Limits per VT-125 brief (orchestrator-agent-specific; tighter than VT-35's
sales_recovery constants of 25 tool calls / 80K tokens / 300s). When any
limit trips, the driver raises ``HardLimitExceeded`` with a structured
terminal envelope so callers can route to ``escalate_to_fazal`` or emit
an explicit failure.

The driver is the canonical invocation seam — direct ``agent.invoke``
bypasses limit tracking. Callers MUST enter
``observability_context(run_id=..., tenant_id=...)`` (VT-181) before
calling ``OrchestratorAgentDriver.invoke()``; the langchain callback
(VT-125's ``OrchestratorReasoningCallback``) attaches per invocation
and writes ``agent_reasoning_step`` rows via VT-180's ``write_step``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from orchestrator.observability.decorators import _observability_context

logger = logging.getLogger(__name__)


# VT-125 limits. Tighter than VT-35 sales_recovery limits — orchestrator-
# agent is the routing brain, not a domain reasoner; bounded per-invocation.
# VT-617: raised 5 -> 10. Under the CL-443 conversational-primary reframe the
# brain (dispatch_brain / route:none) is the PRIMARY surface and legitimately
# does inline multi-tool work in ONE turn — e.g. a multi-field onboarding
# message needs read_onboarding_state + record_answer x3 + next_required_question
# (6 calls). At 5 the run truncated mid-save, the owner saw a "hiccup saving"
# snag, and it then repeated (the multi_field stuck-loop the VT-611 gate flagged).
# Runaway is still bounded by the token (10k), wall-clock (120s), cost (₹5), and
# depth (3) guards below — tool-call COUNT was the redundantly-tight axis for the
# reframed role, not a real cost lever.
ORCHESTRATOR_TOOL_CALL_HARD_LIMIT = 10
ORCHESTRATOR_TOKEN_HARD_LIMIT = 10_000
ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S = 120.0
ORCHESTRATOR_COST_HARD_LIMIT_PAISE = 500  # ₹5
ORCHESTRATOR_DEPTH_HARD_LIMIT = 3


class HardLimitExceeded(RuntimeError):
    """Raised when an orchestrator-agent invocation breaches a hard limit.

    Carries a structured envelope (``axis``, ``observed``, ``limit``,
    ``run_id``, ``tenant_id``) so callers can route to escalation or
    emit a deterministic failure response.
    """

    def __init__(
        self,
        *,
        axis: str,
        observed: int | float,
        limit: int | float,
        run_id: UUID,
        tenant_id: UUID,
    ) -> None:
        self.axis = axis
        self.observed = observed
        self.limit = limit
        self.run_id = run_id
        self.tenant_id = tenant_id
        super().__init__(
            f"orchestrator-agent hard limit: {axis} observed={observed} "
            f"limit={limit} run_id={run_id} tenant_id={tenant_id}"
        )


@dataclass
class OrchestratorUsage:
    """Per-invocation usage tracker.

    Updated by the langchain callback (`on_llm_end` adds tokens + cost;
    `on_tool_start` increments tool_calls). The driver inspects after
    each LLM/tool boundary to detect overshoot.
    """

    tool_calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_paise: int = 0
    started_at: float = field(default_factory=time.monotonic)
    depth: int = 1

    @property
    def cumulative_tokens(self) -> int:
        return self.tokens_input + self.tokens_output

    @property
    def wall_clock_s(self) -> float:
        return time.monotonic() - self.started_at


class OrchestratorAgentDriver:
    """Hard-limit enforcing wrapper around an orchestrator-agent runnable.

    Usage::

        from orchestrator.observability.decorators import observability_context
        from orchestrator.agent.orchestrator_agent import build_orchestrator_agent
        from orchestrator.agent.orchestrator_agent_driver import OrchestratorAgentDriver

        agent = build_orchestrator_agent(model)
        driver = OrchestratorAgentDriver(agent, model_name="claude-opus-4-7")
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            result = driver.invoke(
                messages=[{"role": "user", "content": event_payload}],
                run_id=run_id,
                tenant_id=tenant_id,
                depth=1,
            )
    """

    def __init__(
        self,
        agent: Any,
        *,
        model_name: str,
        tool_call_limit: int = ORCHESTRATOR_TOOL_CALL_HARD_LIMIT,
        token_limit: int = ORCHESTRATOR_TOKEN_HARD_LIMIT,
        wall_clock_limit_s: float = ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S,
        cost_limit_paise: int = ORCHESTRATOR_COST_HARD_LIMIT_PAISE,
        depth_limit: int = ORCHESTRATOR_DEPTH_HARD_LIMIT,
    ) -> None:
        self.agent = agent
        self.model_name = model_name
        self.tool_call_limit = tool_call_limit
        self.token_limit = token_limit
        self.wall_clock_limit_s = wall_clock_limit_s
        self.cost_limit_paise = cost_limit_paise
        self.depth_limit = depth_limit

    def invoke(
        self,
        *,
        messages: list[dict[str, Any]],
        run_id: UUID,
        tenant_id: UUID,
        depth: int = 1,
    ) -> dict[str, Any]:
        """Invoke the orchestrator-agent with hard-limit tracking.

        Raises ``HardLimitExceeded`` on breach. Returns the agent's
        final state dict on success.

        Depth check fires PRE-invocation: a depth-3 spawn cannot start
        a deeper orchestrator turn. Other limits fire mid-invocation
        via the callback's after-LLM/after-tool hooks.
        """
        if depth > self.depth_limit:
            raise HardLimitExceeded(
                axis="depth",
                observed=depth,
                limit=self.depth_limit,
                run_id=run_id,
                tenant_id=tenant_id,
            )

        # Verify ObservabilityContext is set (the langchain callback
        # reads it; without it the callback skips write_step and we
        # lose the agent_reasoning_step row).
        ctx = _observability_context.get()
        if ctx is None:
            logger.warning(
                "OrchestratorAgentDriver invoked without ObservabilityContext; "
                "agent_reasoning_step rows will be skipped (best-effort per CL-122)",
                extra={"run_id": str(run_id), "tenant_id": str(tenant_id)},
            )

        usage = OrchestratorUsage(depth=depth)
        from orchestrator.observability.langchain_callback import (
            OrchestratorReasoningCallback,
        )

        callback = OrchestratorReasoningCallback(
            driver=self,
            usage=usage,
            run_id=run_id,
            tenant_id=tenant_id,
        )

        try:
            result = self.agent.invoke(
                {"messages": messages, "run_id": run_id, "tenant_id": tenant_id},
                config={"callbacks": [callback]},
            )
        except HardLimitExceeded:
            raise
        except Exception as exc:
            logger.error(
                "OrchestratorAgentDriver.invoke unhandled exception",
                extra={
                    "exc": repr(exc),
                    "run_id": str(run_id),
                    "tenant_id": str(tenant_id),
                    "usage": {
                        "tool_calls": usage.tool_calls,
                        "tokens": usage.cumulative_tokens,
                        "wall_clock_s": usage.wall_clock_s,
                        "cost_paise": usage.cost_paise,
                    },
                },
            )
            raise

        # Final wall-clock + cost check after invocation completes (the
        # callback also checks mid-flight; final check covers any
        # post-LLM tool-orchestration overhead langchain adds).
        self._enforce_post_invocation(usage, run_id=run_id, tenant_id=tenant_id)
        return dict(result) if not isinstance(result, dict) else result

    def _enforce_post_invocation(
        self, usage: OrchestratorUsage, *, run_id: UUID, tenant_id: UUID
    ) -> None:
        if usage.wall_clock_s > self.wall_clock_limit_s:
            raise HardLimitExceeded(
                axis="wall_clock_s",
                observed=usage.wall_clock_s,
                limit=self.wall_clock_limit_s,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.cost_paise > self.cost_limit_paise:
            raise HardLimitExceeded(
                axis="cost_paise",
                observed=usage.cost_paise,
                limit=self.cost_limit_paise,
                run_id=run_id,
                tenant_id=tenant_id,
            )

    def check_mid_invocation(
        self, usage: OrchestratorUsage, *, run_id: UUID, tenant_id: UUID
    ) -> None:
        """Called by the callback after each LLM/tool boundary.

        Raises ``HardLimitExceeded`` on breach. The exception propagates
        through langchain's callback machinery and aborts the agent run.
        """
        if usage.tool_calls > self.tool_call_limit:
            raise HardLimitExceeded(
                axis="tool_calls",
                observed=usage.tool_calls,
                limit=self.tool_call_limit,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.cumulative_tokens > self.token_limit:
            raise HardLimitExceeded(
                axis="tokens",
                observed=usage.cumulative_tokens,
                limit=self.token_limit,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.wall_clock_s > self.wall_clock_limit_s:
            raise HardLimitExceeded(
                axis="wall_clock_s",
                observed=usage.wall_clock_s,
                limit=self.wall_clock_limit_s,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        if usage.cost_paise > self.cost_limit_paise:
            raise HardLimitExceeded(
                axis="cost_paise",
                observed=usage.cost_paise,
                limit=self.cost_limit_paise,
                run_id=run_id,
                tenant_id=tenant_id,
            )


__all__ = [
    "HardLimitExceeded",
    "OrchestratorAgentDriver",
    "OrchestratorUsage",
    "ORCHESTRATOR_TOOL_CALL_HARD_LIMIT",
    "ORCHESTRATOR_TOKEN_HARD_LIMIT",
    "ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S",
    "ORCHESTRATOR_COST_HARD_LIMIT_PAISE",
    "ORCHESTRATOR_DEPTH_HARD_LIMIT",
]
