"""VT-125 OrchestratorAgentDriver — hard-limit enforcement for orchestrator-agent.

Wraps the langchain `create_agent` runnable returned by
`build_orchestrator_agent` so each invocation tracks:

- **Tool calls:** 5 per invocation
- **Output tokens:** 8,000 (VT-764 — input is prompt size, not a runaway signal)
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
# Runaway is still bounded by the output-token (8k, VT-764), wall-clock (120s), cost (₹5), and
# depth (3) guards below — tool-call COUNT was the redundantly-tight axis for the
# reframed role, not a real cost lever.
ORCHESTRATOR_TOOL_CALL_HARD_LIMIT = 10

# VT-764: the token axis is OUTPUT-ONLY, and 10_000 combined is gone.
#
# The old limit counted ``tokens_input + tokens_output`` against 10_000. The brain's prompt alone
# measures 16,780 tokens at p50 (17,656 max) on dev over 7 days, so the check was TRUE on the first
# call of EVERY run — and it stopped nothing, because the raise happened inside a langchain callback
# whose exceptions the callback manager caught and logged. A guard that fires every run and aborts
# none is worse than no guard: it was the loudest line in the log window while the runs it "guarded"
# completed at 86.8%.
#
# Input tokens are PROMPT SIZE — a deploy-time property, not a runaway signal. A brain that loops or
# generates endlessly shows it in OUTPUT, tool calls, wall clock and cost. Prompt growth is a real
# concern with its own gate (``gate-sr-agent-prompt-token-cap``); it does not belong on a runaway
# axis.
#
# MEASURED on deployed dev, `llm_call_events`, 7 days to 2026-08-17 — output tokens per invocation
# (tenant × call_site × minute), so the next reader does not have to trust an adjective:
#
#     call_site           n     out p50   out p95   out max    (in+out max)
#     complex (brain)     432        42       359       728         68,184
#     sr_draft_turn       338     1,140     2,995     4,946         24,575
#     gap_compose         195     1,477     1,803     2,238          3,528
#     self_evaluate_gate  190       547     1,137     1,732          6,138
#     turn_brain_tools    202       351       702       904          9,248
#     triage              980        72       214       490          4,667
#
# 8_000 sits ~11x above the brain's observed max (728) and ~1.6x above the largest observed
# invocation output anywhere in the system (4,946), so a legitimate multi-tool turn cannot trip it
# while a genuine runaway generation will. Note the in+out max of 68,184 for the brain: the retired
# limit sat 6.8x UNDER real observed spend, which is the measurement that shows it was never a
# runaway threshold at all. Raise this WITH a fresh query, never on a hunch.
ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT = 8_000
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

        # model_name is for COST ATTRIBUTION only — the driver never selects a model. Take it from
        # the seam (VT-732) so the docstring cannot teach a hardcoded id back into the codebase.
        from orchestrator.llm import resolve_model_id

        agent = build_orchestrator_agent(model)
        driver = OrchestratorAgentDriver(agent, model_name=resolve_model_id("complex"))
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
        output_token_limit: int = ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT,
        wall_clock_limit_s: float = ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S,
        cost_limit_paise: int = ORCHESTRATOR_COST_HARD_LIMIT_PAISE,
        depth_limit: int = ORCHESTRATOR_DEPTH_HARD_LIMIT,
    ) -> None:
        self.agent = agent
        self.model_name = model_name
        self.tool_call_limit = tool_call_limit
        self.output_token_limit = output_token_limit
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
        if usage.tokens_output > self.output_token_limit:
            raise HardLimitExceeded(
                axis="tokens_output",
                observed=usage.tokens_output,
                limit=self.output_token_limit,
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
    "ORCHESTRATOR_OUTPUT_TOKEN_HARD_LIMIT",
    "ORCHESTRATOR_WALL_CLOCK_HARD_LIMIT_S",
    "ORCHESTRATOR_COST_HARD_LIMIT_PAISE",
    "ORCHESTRATOR_DEPTH_HARD_LIMIT",
]
