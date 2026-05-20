"""Parent multi-agent StateGraph wiring (VT-3.4 PR 1/3 + 2/3).

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
"""

from __future__ import annotations

import json
from typing import Any

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from orchestrator.agent.orchestrator_agent import build_orchestrator_agent
from orchestrator.agent.sales_recovery_stub import (
    build_stub_sales_recovery_agent,
    hardcoded_campaign_plan,
)
from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan
from orchestrator.collapse import collapse_node
from orchestrator.handoffs import spawn_sales_recovery
from orchestrator.routing import orchestrator_terminal_node, route_after_orchestrator
from orchestrator.state.agent_graph_state import AgentGraphState


def build_supervisor_graph(
    model: ChatAnthropic,
    checkpointer: PostgresSaver | None = None,
) -> Any:
    """Compose and compile the parent multi-agent graph.

    Nodes:
      - orchestrator_agent: the supervisor, built with spawn_sales_recovery
        added to its tools.
      - sales_recovery_agent: a node wrapping the stub specialist; it parses a
        CampaignPlan from the stub's final message (hardcoded fallback).
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
    sales_recovery = build_stub_sales_recovery_agent(model=model)

    def sales_recovery_node(state: AgentGraphState) -> dict[str, Any]:
        """Wrap the stub agent. Parse a CampaignPlan from the final message;
        fall back to the hardcoded plan on any parse failure.

        VT-122: ``CampaignPlan`` is now a v1.0 discriminated union over
        ``status``; ``parse_campaign_plan`` is the TypeAdapter accessor
        (the union has no ``model_validate``). On any parse failure
        (malformed JSON, schema violation, wrong variant shape) we fall
        back to the hardcoded proposed variant.

        Tenant + run identity (CL-202 / Pillar 3): the run's tenant_id
        and run_id (carried in AgentGraphState) are the authoritative
        boundary. The specialist's emitted plan may carry placeholders;
        overwrite both fields here so downstream sees a single source
        of truth."""
        result = sales_recovery.invoke({"messages": state["messages"]})
        final_content = result["messages"][-1].content

        try:
            # final_content may be a string or a list of content blocks
            # depending on the model's output shape — normalise to a string.
            if isinstance(final_content, list):
                final_content = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in final_content
                )
            plan = parse_campaign_plan(json.loads(final_content))
        except Exception:
            plan = hardcoded_campaign_plan()

        overrides: dict[str, Any] = {}
        state_tenant_id = state.get("tenant_id")
        if state_tenant_id is not None and plan.tenant_id != state_tenant_id:
            overrides["tenant_id"] = state_tenant_id
        state_run_id = state.get("run_id")
        if state_run_id is not None and plan.run_id != state_run_id:
            overrides["run_id"] = state_run_id
        if overrides:
            plan = plan.model_copy(update=overrides)

        return {"messages": result["messages"], "campaign_plan": plan}

    graph = StateGraph(AgentGraphState)
    graph.add_node("orchestrator_agent", orchestrator)
    graph.add_node("sales_recovery_agent", sales_recovery_node)
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
