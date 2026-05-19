"""Parent multi-agent StateGraph wiring (VT-3.4 PR 1/3).

Per CL-175: built manually instead of using ``langgraph_supervisor.create_supervisor``.
``orchestrator_agent`` IS the supervisor (CL-22). Specialists are routed-to via
custom handoff tools returning ``Command(goto=..., graph=Command.PARENT)``.

PATTERN NOTE: this is the first place ``Command.PARENT`` routing executes in
our codebase outside the langgraph_supervisor library. The primitives
(``Command``, ``StateGraph``) are documented; the COMPOSITION is novel here.
The PR 1/3 happy-path test (``tests/orchestrator/test_supervisor.py``) is the
canary.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, START, StateGraph

from orchestrator.agent.orchestrator_agent import build_orchestrator_agent
from orchestrator.agent.sales_recovery_stub import (
    build_stub_sales_recovery_agent,
    hardcoded_campaign_plan,
)
from orchestrator.handoffs import spawn_sales_recovery
from orchestrator.state.agent_graph_state import AgentGraphState
from orchestrator.types.campaign_plan import CampaignPlan


def build_supervisor_graph(model: ChatAnthropic) -> Any:
    """Compose and compile the parent multi-agent graph.

    Nodes:
      - orchestrator_agent: the supervisor, built with spawn_sales_recovery
        added to its tools.
      - sales_recovery_agent: a node wrapping the stub specialist; it parses a
        CampaignPlan from the stub's final message (hardcoded fallback).

    Routing:
      - START -> orchestrator_agent
      - orchestrator_agent emits Command(goto='sales_recovery_agent',
        graph=Command.PARENT) via the spawn_sales_recovery tool — an implicit
        edge, so no add_edge is needed for that transition.
      - sales_recovery_agent -> END
    """
    orchestrator = build_orchestrator_agent(model=model, extra_tools=[spawn_sales_recovery])
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
    graph.add_edge(START, "orchestrator_agent")
    graph.add_edge("sales_recovery_agent", END)
    # orchestrator_agent -> sales_recovery_agent is implicit via the
    # Command(goto='sales_recovery_agent') the spawn_sales_recovery tool emits.

    return graph.compile()
