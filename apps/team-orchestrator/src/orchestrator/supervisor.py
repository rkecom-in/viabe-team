"""Parent multi-agent StateGraph wiring (VT-3.4 PR 1/3 + 2/3, dispatch-switch
VT-SalesRecovery-Agent Exec Order 6.7).

Per CL-175: built manually instead of using ``langgraph_supervisor.create_supervisor``.
``orchestrator_agent`` IS the supervisor (CL-22). Specialists are routed-to via
custom handoff tools returning ``Command(goto=..., graph=Command.PARENT)``.

PR 2/3 (CL-188): adds an explicit conditional edge after the orchestrator —
``route_after_orchestrator`` sends the spawn case to ``sales_recovery_agent``
and the no-spawn case to the ``orchestrator_terminal`` sink. Also accepts an
optional ``checkpointer``.

CL-183 VERIFICATION TARGET (verified in test_supervisor.py):
``Command.PARENT`` from the spawn tool vs the ``add_conditional_edges`` after
the orchestrator node — the precedence of these two is NOT documented in
Context7 for this composition. The landmine test exercises both paths and
asserts the observed behaviour. Do not remove it as "redundant".

Dispatch switch (this commit): the ``sales_recovery_agent`` node now calls
``run_sales_recovery_agent`` (VT-32) instead of the langchain ``create_agent``
stub. The self-evaluate gate (VT-36 + VT-50 + the VT-SR-Agent wiring) is
construction-injected via a per-run ``SelfEvaluateAdapter`` and becomes
PRODUCTION-LOAD-BEARING with this PR. The stub module remains on disk for
out-of-graph callers (tests, future replay tooling) but is no longer on the
dispatch path.

Module-level node (NOT a closure) so tests can ``monkeypatch.setattr(supervisor_mod,
"_sales_recovery_node", ...)`` the same way collapse_node is patched in the
landmine routing tests.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from team_shared.mcp import ToolContext

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.limits.wallclock_timer import WALL_CLOCK_HARD_LIMIT_S
from orchestrator.agent.orchestrator_agent import build_orchestrator_agent
from orchestrator.agent.sales_recovery import (
    SalesRecoveryContext,
    run_sales_recovery_agent,
)
from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
from orchestrator.agent.tools.self_evaluate import SelfEvaluateAdapter
from orchestrator.collapse import collapse_node
from orchestrator.db import tenant_connection
from orchestrator.handoffs import spawn_sales_recovery
from orchestrator.routing import orchestrator_terminal_node, route_after_orchestrator
from orchestrator.state.agent_graph_state import AgentGraphState


# Per-run budgets sourced from VT-35's hard-limit constants. Matched to the
# values agent/sales_recovery_node.py uses for the standalone-node path so
# the supervisor dispatch and the standalone wrapper give the gate the same
# context shape.
_RUN_COST_BUDGET_PAISE = 5_000  # ₹50 per VT-35
_RUN_WALLCLOCK_BUDGET_MS = int(WALL_CLOCK_HARD_LIMIT_S * 1000)


def _extract_user_request(state: AgentGraphState) -> str:
    """Read the first HumanMessage content from ``state['messages']``.

    The orchestrator's user input lives at index 0 (langgraph
    ``add_messages`` preserves order). Fail loud if absent or not a
    HumanMessage — the specialist must not be spawned without one. The
    extracted string flows into ``SalesRecoveryContext.user_request``
    (CL-287) and becomes the agent loop's initial user message.

    Tolerates two on-disk message shapes:
      - ``HumanMessage`` instances (post add_messages reducer)
      - bare dicts ``{"role": "user", "content": "..."}`` (pre-reducer
        — the invoke() seed shape used by some test fixtures)
    """
    messages = state.get("messages") or []
    if not messages:
        raise ValueError(
            "sales_recovery_node: state['messages'] empty —"
            " orchestrator must spawn the specialist with a user request"
        )
    first = messages[0]
    if isinstance(first, HumanMessage):
        content = first.content
    elif isinstance(first, dict) and first.get("role") == "user":
        content = first.get("content", "")
    else:
        raise ValueError(
            "sales_recovery_node: state['messages'][0] is not a user"
            f" message (got {type(first).__name__})"
        )
    if isinstance(content, list):
        # langchain content can be a list of blocks; concat any text parts.
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            "sales_recovery_node: user request is empty"
        )
    return content


def _sales_recovery_node(state: AgentGraphState) -> dict[str, Any]:
    """The supervisor's specialist-dispatch node.

    Calls ``run_sales_recovery_agent`` (VT-32) — the REAL agent loop on the
    Anthropic Messages SDK with the self-evaluate gate active (VT-36, made
    structural by VT-SR-Agent gate wiring; backed by VT-50's Opus evaluator).

    Tenant + run identity (CL-202 / Pillar 3): tenant_id and run_id MUST
    be present in state — fail loud. The state's values are the
    authoritative boundary; the agent's emitted plan may carry placeholder
    UUIDs which we overwrite before returning.

    Parse exception handling (CL-238): catches only
    ``(json.JSONDecodeError, ValidationError)`` — narrow by design. A
    ``ValidationError`` from a live agent indicates the agent emitted a
    malformed CampaignPlan; that must NOT be swallowed as a clean miss
    (would mask a real bug). The exception re-raises; the graph run
    fails and DBOS / the error router observes it.
    """
    state_tenant_id = state.get("tenant_id")
    if state_tenant_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: tenant_id missing from state"
        )
    state_run_id = state.get("run_id")
    if state_run_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: run_id missing from state"
        )

    tenant_uuid = (
        state_tenant_id
        if isinstance(state_tenant_id, UUID)
        else UUID(str(state_tenant_id))
    )
    run_uuid = (
        state_run_id
        if isinstance(state_run_id, UUID)
        else UUID(str(state_run_id))
    )

    # CL-287: thread the orchestrator's user request to the specialist.
    # The specialist's hardcoded "begin" cue was a VT-32 placeholder; v1.0
    # prompt (VT-33/VT-4.2) needs a real task to reason about. Pull the
    # first HumanMessage content from state["messages"] — that is the
    # orchestrator graph's user input (langgraph add_messages reducer
    # preserves it at index 0). Fail loud if absent: the specialist must
    # never be spawned without a user request.
    user_request = _extract_user_request(state)

    # Per-invocation ToolContext + adapter — the gate runs against a real
    # SelfEvaluateAdapter (Opus-backed by VT-50). Production-load-bearing
    # path activates here.
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
    agent_result = run_sales_recovery_agent(context, evaluator=evaluator)

    if agent_result.output is None:
        # Live-agent terminal failure modes (status in {refused, invalid,
        # terminated}) produce no output. The agent's own emit calls
        # routed a FailureRecord; the supervisor surfaces the failure
        # rather than synthesising a fallback plan (CL-238 — the brief's
        # "real error, not silent fallback").
        raise RuntimeError(
            f"sales_recovery_node: agent returned status={agent_result.status!r}"
            " with no output (FailureRecord already routed if applicable)"
        )

    # Tight exception handling — narrow catch on parse failure. A
    # ValidationError surfacing here means the live agent emitted a
    # malformed CampaignPlan; that is a real bug, not a degraded run.
    try:
        plan = parse_campaign_plan(agent_result.output)
    except (json.JSONDecodeError, ValidationError):
        raise

    overrides: dict[str, Any] = {}
    if plan.tenant_id != tenant_uuid:
        overrides["tenant_id"] = tenant_uuid
    if plan.run_id != run_uuid:
        overrides["run_id"] = run_uuid
    if overrides:
        plan = plan.model_copy(update=overrides)

    return {"campaign_plan": plan}


def build_supervisor_graph(
    model: ChatAnthropic,
    checkpointer: PostgresSaver | None = None,
) -> Any:
    """Compose and compile the parent multi-agent graph.

    Nodes:
      - orchestrator_agent: the supervisor, built with spawn_sales_recovery
        added to its tools.
      - sales_recovery_agent: the module-level ``_sales_recovery_node`` —
        calls the REAL ``run_sales_recovery_agent`` with the self-evaluate
        gate active (VT-SR-Agent dispatch switch).
      - orchestrator_terminal: the no-spawn sink (CL-188).

    Routing:
      - START -> orchestrator_agent
      - orchestrator_agent -> conditional: 'spawn' -> sales_recovery_agent,
        'terminal' -> orchestrator_terminal (route_after_orchestrator).
        The spawn tool ALSO emits Command(goto='sales_recovery_agent',
        graph=Command.PARENT) — landmine test covers the precedence.
      - sales_recovery_agent -> collapse (PR 3/3): persists the CampaignPlan
        and updates subscriber_states activity. No phase change.
      - collapse -> END
      - orchestrator_terminal -> END

    ``checkpointer`` (PR 2/3): when given, the graph compiles with Postgres
    checkpointing; PR 1/3 callers pass nothing and compile checkpoint-free.
    """
    orchestrator = build_orchestrator_agent(
        model=model, extra_tools=[spawn_sales_recovery]
    )

    graph = StateGraph(AgentGraphState)
    graph.add_node("orchestrator_agent", orchestrator)
    graph.add_node("sales_recovery_agent", _sales_recovery_node)
    graph.add_node("collapse", collapse_node)
    graph.add_node("orchestrator_terminal", orchestrator_terminal_node)
    graph.add_edge(START, "orchestrator_agent")
    graph.add_conditional_edges(
        "orchestrator_agent",
        route_after_orchestrator,
        {"spawn": "sales_recovery_agent", "terminal": "orchestrator_terminal"},
    )
    graph.add_edge("sales_recovery_agent", "collapse")
    graph.add_edge("collapse", END)
    graph.add_edge("orchestrator_terminal", END)

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
