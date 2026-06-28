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

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.context_builder import build_sales_recovery_context
from orchestrator.types.trigger_reason import TriggerReason


def _extract_user_request_from_state(state: dict[str, Any]) -> str:
    """Pull the first HumanMessage content from ``state['messages']``.

    Exec-6.85: the Composer now carries ``user_request`` inside the bundle,
    so the handoff must extract it at the spawn site (instead of letting
    the specialist node do it post-handoff). Tolerates the two on-disk
    message shapes used by the supervisor graph + the invoke() seed shape.
    """
    messages = state.get("messages") or []
    if not messages:
        raise ValueError(
            "spawn_sales_recovery: state['messages'] is empty —"
            " orchestrator must spawn the specialist with a user request"
        )
    # The dispatch path prepends SystemMessage blocks (L1 / business-context /
    # manager-intent), so the user message is NOT necessarily messages[0].
    # Scan for the FIRST HumanMessage / role='user' (matches the docstring) —
    # indexing [0] crashed the spawn once dispatch started prepending (VT-463).
    content: Any = None
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            break
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            break
    if content is None:
        raise ValueError(
            "spawn_sales_recovery: no user message in state['messages']"
            " (only system/non-user messages present)"
        )
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("spawn_sales_recovery: user request is empty")
    return content


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

    # Exec-6.85: user_request is now part of the bundle. Extract at the
    # handoff site rather than re-extracting in the specialist node.
    user_request = _extract_user_request_from_state(state)

    bundle = build_sales_recovery_context(
        tenant_id=tenant_id,
        run_id=run_id,
        trigger_reason=trigger_reason,
        user_request=user_request,
    )
    return {"sales_recovery_context": bundle}


def _build_integration_update(state: dict[str, Any]) -> dict[str, Any]:
    """Build the ``spawn_integration`` Command.update extension (VT-206).

    Minimal: integration agent reads everything from tenant_integration_state
    on its own; no specialist bundle needed at handoff time. Fail-loud
    on missing tenant_id / run_id per CL-195 + Pillar 3.
    """
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError(
            "spawn_integration: tenant_id missing from state"
        )
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError(
            "spawn_integration: run_id missing from state"
        )
    return {}


def _build_onboarding_conductor_update(state: dict[str, Any]) -> dict[str, Any]:
    """Build the ``spawn_onboarding_conductor`` Command.update extension (VT-462).

    Minimal: the conductor reads the tenant's draft + journey state on its own (its tools key on
    tenant_id); no specialist bundle needed at handoff. Fail-loud on missing tenant_id / run_id per
    CL-195 + Pillar 3 (parity with ``_build_integration_update``)."""
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError(
            "spawn_onboarding_conductor: tenant_id missing from state"
        )
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError(
            "spawn_onboarding_conductor: run_id missing from state"
        )
    return {}


spawn_integration = make_spawn_tool(
    agent_name="integration_agent",
    tool_name="spawn_integration",
    description=(
        "Hand off to the Integration Agent for owner onboarding "
        "(connecting Shopify / Google Sheets / etc.). Use when the "
        "conversation indicates the owner wants to add or configure a "
        "data source."
    ),
    update_builder=_build_integration_update,
)


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


spawn_onboarding_conductor = make_spawn_tool(
    agent_name="onboarding_conductor",
    tool_name="spawn_onboarding_conductor",
    description=(
        "Hand off to the Onboarding-Conductor for the owner's PROFILE-SETUP "
        "conversation (confirming the discovered business profile + collecting "
        "the missing business-context fields). Use when the owner is new or "
        "mid-onboarding and the next step is setting up their business profile "
        "— BEFORE connecting a data source (which is the Integration Agent)."
    ),
    update_builder=_build_onboarding_conductor_update,
)
