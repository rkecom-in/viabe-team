"""Custom handoff tools for specialist routing (VT-3.4 PR 1/3).

Per CL-175: we use ``langgraph.types.Command`` directly instead of
``langgraph_supervisor.create_handoff_tool``. This file owns the ``spawn_*``
factory for all specialists. Phase 2-6 adds spawn_reputation, spawn_marketing,
etc. as additional functions here.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command


def make_spawn_tool(
    *,
    agent_name: str,
    tool_name: str,
    description: str,
) -> BaseTool:
    """Factory for specialist handoff tools.

    The returned tool, when invoked by an agent's LLM, returns a Command that
    routes the parent StateGraph to the specialist node. The node name in the
    parent graph MUST equal ``agent_name``.

    PR 1/3 keeps the update payload minimal (messages + active_agent). PR 2/3
    will add task_description for Context Composer integration.
    """

    @tool(tool_name, description=description)
    def handoff(
        state: Annotated[dict[str, Any], InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[Any]:
        tool_message = ToolMessage(
            content=f"Handing off to {agent_name}",
            name=tool_name,
            tool_call_id=tool_call_id,
        )
        return Command(
            goto=agent_name,
            graph=Command.PARENT,
            update={
                "messages": state["messages"] + [tool_message],
                "active_agent": agent_name,
            },
        )

    return handoff


spawn_sales_recovery = make_spawn_tool(
    agent_name="sales_recovery_agent",
    tool_name="spawn_sales_recovery",
    description=(
        "Hand off to the Sales Recovery Agent for dormant-customer "
        "winback campaign work. Use when the conversation indicates "
        "the owner wants to recover sales from inactive customers."
    ),
)
