"""Custom handoff tools for specialist routing (VT-3.4 PR 1/3 + PR 2/3).

Per CL-175: we use ``langgraph.types.Command`` directly instead of
``langgraph_supervisor.create_handoff_tool``. This file owns the ``spawn_*``
factory for all specialists. Phase 2-6 adds spawn_reputation, spawn_marketing,
etc. as additional functions here.

PR 2/3 (CL-209): ``make_spawn_tool`` gains an optional ``update_builder`` hook
so a specialist handoff can attach a context bundle to its Command.update.
``spawn_sales_recovery`` uses it to build + attach a ``SalesRecoveryContext``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.context_builder import build_sales_recovery_context
from orchestrator.types.trigger_reason import TriggerReason


def make_spawn_tool(
    *,
    agent_name: str,
    tool_name: str,
    description: str,
    update_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> BaseTool:
    """Factory for specialist handoff tools.

    The returned tool, when invoked by an agent's LLM, returns a Command that
    routes the parent StateGraph to the specialist node. The node name in the
    parent graph MUST equal ``agent_name``.

    ``update_builder`` (PR 2/3): an optional hook producing specialist-specific
    Command.update fields. When given, its result is merged on top of the base
    ``{messages, active_agent}`` payload. ``spawn_sales_recovery`` uses it to
    attach the ``SalesRecoveryContext`` bundle; generic spawns omit it.
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
        update: dict[str, Any] = {
            "messages": state["messages"] + [tool_message],
            "active_agent": agent_name,
        }
        if update_builder is not None:
            update.update(update_builder(state))
        return Command(goto=agent_name, graph=Command.PARENT, update=update)

    return handoff


def _build_sales_recovery_update(state: dict[str, Any]) -> dict[str, Any]:
    """Build the ``spawn_sales_recovery`` Command.update extension (VT-3.4 PR 2/3).

    Reads run identity from the orchestrator agent's state — propagated into
    the subgraph via ``OrchestratorAgentState`` (CL-209 seam fix) — and attaches
    a ``SalesRecoveryContext`` bundle for the specialist.

    Fail-loud (Pillar 3 / CL-195): ``tenant_id`` and ``run_id`` MUST be present.
    A missing value means an upstream producer never populated state — a silent
    fallback would be a tenant-scoping bug, so raise ``TenantIsolationError``.
    ``trigger_reason`` is different by design: it carries an explicit read-site
    fallback (CL-195), so it is NOT None-checked here.
    """
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError(
            "spawn_sales_recovery: tenant_id missing from state"
        )
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError(
            "spawn_sales_recovery: run_id missing from state"
        )

    # CL-195: the 'weekly_cadence' fallback lives HERE, at the read site —
    # the state schema keeps trigger_reason defaulting to None so a missing
    # upstream source stays observable rather than masked.
    trigger_reason: TriggerReason = state.get("trigger_reason") or "weekly_cadence"

    bundle = build_sales_recovery_context(
        tenant_id=tenant_id,
        run_id=run_id,
        trigger_reason=trigger_reason,
    )
    return {"sales_recovery_context": bundle}


spawn_sales_recovery = make_spawn_tool(
    agent_name="sales_recovery_agent",
    tool_name="spawn_sales_recovery",
    description=(
        "Hand off to the Sales Recovery Agent for dormant-customer "
        "winback campaign work. Use when the conversation indicates "
        "the owner wants to recover sales from inactive customers."
    ),
    update_builder=_build_sales_recovery_update,
)
