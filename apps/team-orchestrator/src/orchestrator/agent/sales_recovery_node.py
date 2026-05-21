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
from uuid import UUID

from team_shared.mcp import ToolContext

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.limits.wallclock_timer import WALL_CLOCK_HARD_LIMIT_S
from orchestrator.agent.sales_recovery import (
    SalesRecoveryContext,
    run_sales_recovery_agent,
)
from orchestrator.agent.tools.self_evaluate import SelfEvaluateAdapter
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

    Fail-loud (Pillar 3 / CL-202): ``tenant_id`` and ``run_id`` MUST be
    present in ``state``. Constructs a ``SelfEvaluateAdapter`` per
    invocation (one ToolContext per run) and passes it into
    ``run_sales_recovery_agent`` so the self-evaluate gate runs at every
    terminal draft.

    The returned dict places the result under ``agent_result`` as a
    plain mapping (LangGraph state values must be reducer-friendly).
    """
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: tenant_id missing from state"
        )
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: run_id missing from state"
        )
    # CL-287: standalone wrapper's input contract — state must carry the
    # orchestrator-supplied user request. The supervisor's
    # ``_sales_recovery_node`` extracts it from ``state['messages']``;
    # this wrapper (used outside the supervisor graph) takes it directly
    # under ``state['user_request']``. Required, no default.
    user_request = state.get("user_request")
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError(
            "sales_recovery_node: user_request missing or empty in state"
        )

    tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))

    # Per-invocation ToolContext for VT-50's adapter. db_handle bridges
    # to orchestrator.db.tenant_connection until VT-8.1 ships typed
    # wrappers (cf. VT-39 framework doc).
    tool_ctx = ToolContext(
        tenant_id=tenant_uuid,
        run_id=run_uuid,
        agent_id="sales_recovery",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=_RUN_COST_BUDGET_PAISE,
        wallclock_remaining_ms=_RUN_WALLCLOCK_BUDGET_MS,
        db_handle=tenant_connection,
    )
    evaluator = SelfEvaluateAdapter(ctx=tool_ctx)

    context = SalesRecoveryContext(
        tenant_id=str(tenant_uuid),
        run_id=str(run_uuid),
        user_request=user_request,
    )
    result = run_sales_recovery_agent(context, evaluator=evaluator)
    return {"agent_result": asdict(result)}


__all__ = ["sales_recovery_node"]
