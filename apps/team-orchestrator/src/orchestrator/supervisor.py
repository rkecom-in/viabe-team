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
from orchestrator.handoffs import spawn_sales_recovery
from orchestrator.routing import orchestrator_terminal_node, route_after_orchestrator
from orchestrator.state.agent_graph_state import AgentGraphState
from orchestrator.types.campaign_plan import CampaignPlan


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
      - sales_recovery_agent -> END
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
        fall back to the hardcoded plan on any parse failure."""
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
            plan = CampaignPlan.model_validate(json.loads(final_content))
        except Exception:
            plan = hardcoded_campaign_plan()

        return {"messages": result["messages"], "campaign_plan": plan}

    graph = StateGraph(AgentGraphState)
    graph.add_node("orchestrator_agent", orchestrator)
    graph.add_node("sales_recovery_agent", sales_recovery_node)
    graph.add_node("orchestrator_terminal", orchestrator_terminal_node)
    graph.add_edge(START, "orchestrator_agent")
    graph.add_conditional_edges(
        "orchestrator_agent",
        route_after_orchestrator,
        {"spawn": "sales_recovery_agent", "terminal": "orchestrator_terminal"},
    )
    graph.add_edge("sales_recovery_agent", END)
    graph.add_edge("orchestrator_terminal", END)

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
