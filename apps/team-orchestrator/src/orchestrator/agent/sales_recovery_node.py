"""LangGraph node wrapper for the sales_recovery specialist (VT-32 +
self-evaluate-gate wiring).

Imports and calls ``run_sales_recovery_agent``; translates the returned
``AgentResult`` into a LangGraph state update. Constructs the production
``SelfEvaluateAdapter`` (VT-50) per invocation and injects it into the
gate — until this PR landed, production callers passed nothing and the
gate was inert.

The supervisor's dispatch still routes through the langchain stub
specialist on main (see ``supervisor.py``); a separate dispatch-switch
subtask flips it to call this node. When that lands, the gate becomes
load-bearing in production.

Test injection seam: ``run_sales_recovery_agent`` retains an
``evaluator: SelfEvaluator | None = None`` keyword parameter. Production
callers (this module) always pass a real adapter; ``None`` is reserved
for the unit-test injection seam.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from team_shared.mcp import ToolContext

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.limits.wallclock_timer import WALL_CLOCK_HARD_LIMIT_S
from orchestrator.agent.sales_recovery import run_sales_recovery_agent
from orchestrator.agent.tools.self_evaluate import SelfEvaluateAdapter
from orchestrator.context_builder import build_self_evaluate_context_summary
from orchestrator.db import tenant_connection


# Per-run budgets exposed to the tool layer via ToolContext. Sourced
# from VT-35's hard-limit constants: cost ceiling ₹50 (5000 paise),
# wallclock 300s = 300_000ms. The orchestrator decrements these for
# multi-tool runs; for sales_recovery's single-specialist invocation
# they start at the full per-run cap.
_RUN_COST_BUDGET_PAISE = 5_000  # ₹50 per VT-35
_RUN_WALLCLOCK_BUDGET_MS = int(WALL_CLOCK_HARD_LIMIT_S * 1000)


def sales_recovery_node(state: Mapping[str, Any]) -> dict[str, Any]:
    """Run the specialist; return a state update carrying the AgentResult.

    Exec-6.85: consumes the Context Composer bundle from
    ``state['sales_recovery_context']``. The bundle carries tenant_id,
    run_id, user_request, trigger_reason, and the Composer's per-section
    payload. A missing bundle at this seam means the caller did not
    invoke the Composer — fail loud rather than running the specialist
    against no task context.

    Constructs a ``SelfEvaluateAdapter`` per invocation (one ToolContext
    per run) and passes it into ``run_sales_recovery_agent`` so the
    self-evaluate gate runs at every terminal draft.

    The returned dict places the result under ``agent_result`` as a
    plain mapping (LangGraph state values must be reducer-friendly).
    """
    context = state.get("sales_recovery_context")
    if context is None:
        raise TenantIsolationError(
            "sales_recovery_node: state['sales_recovery_context'] is None —"
            " caller must attach the Context Composer bundle (either via"
            " spawn_sales_recovery in the supervisor graph or via"
            " build_sales_recovery_context for out-of-graph callers)."
        )

    # Per-invocation ToolContext for VT-50's adapter. db_handle bridges
    # to orchestrator.db.tenant_connection until VT-8.1 ships typed
    # wrappers (cf. VT-39 framework doc).
    tool_ctx = ToolContext(
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        agent_id="sales_recovery",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=_RUN_COST_BUDGET_PAISE,
        wallclock_remaining_ms=_RUN_WALLCLOCK_BUDGET_MS,
        db_handle=tenant_connection,
    )
    # VT-485: feed the gate the real grounding context derived from the bundle
    # (cohort distribution, recency basis, expected-ARRR target) instead of the
    # old hardcoded ``{}``. The gate's ``consistency`` category needs this
    # substrate to verify a draft's target_cohort / expected_arrr are grounded —
    # without it a legitimately-grounded win-back and a fabricated one are
    # indistinguishable to the gate. Compact subset only (no PII, no reasoning).
    context_summary = build_self_evaluate_context_summary(context)
    evaluator = SelfEvaluateAdapter(ctx=tool_ctx, context_summary=context_summary)

    result = run_sales_recovery_agent(context, evaluator=evaluator)
    return {"agent_result": asdict(result)}


__all__ = ["sales_recovery_node"]
