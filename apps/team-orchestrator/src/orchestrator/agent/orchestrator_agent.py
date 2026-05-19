"""Orchestrator-Agent — the supervisor of the multi-agent graph (VT-3.9 / VT-3.4).

An Opus 4.7 agent built with langchain ``create_agent`` and the reviewed system
prompt. ``build_orchestrator_agent`` is the factory: callers pass ``extra_tools``
to add context-specific tools (VT-3.4's supervisor passes the ``spawn_sales_recovery``
handoff tool — a specialist handoff is only meaningful inside the parent graph,
so it is NOT in the base tool set).

The module-level ``orchestrator_agent`` is the default-built instance (base tools
only); it is the importable handle used by tests and any standalone caller.

Deferred to later VT-3.9 PRs: L0 memory, the Context Composer bundle, the real
compose_owner_output / send_whatsapp_template / get_subscriber_state /
query_pipeline_history tools, and machine-enforced hard limits.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import BaseTool, tool

from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "orchestrator_agent_system.md"
ORCHESTRATOR_AGENT_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# Pinned exactly in pyproject (langgraph / langchain-* == ): the agent's model
# behaviour is version-sensitive, so model + library bumps are Type 2 changes.
# The call-arg ignore below is needed because mypy --strict does not expand
# ChatAnthropic's pydantic fields into __init__ kwargs without the pydantic
# plugin; the call is valid at runtime (smoke-tested) and a repo-wide mypy
# plugin change is out of scope for this PR.
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


@tool
def escalate_to_fazal(run_id: str, reason: str, context: str) -> str:
    """Escalate to Fazal. Log-only in this skeleton; real wiring is VT-3.6."""
    logger.warning("ESCALATE_TO_FAZAL run_id=%s reason=%s context=%s", run_id, reason, context)
    return f"[skeleton] escalation logged for run_id={run_id}"


# Base tools every orchestrator-agent has, regardless of context. Specialist
# handoff tools (spawn_*) are NOT here — they are passed as extra_tools by the
# supervisor graph, since a handoff is only valid inside the parent graph.
ORCHESTRATOR_AGENT_TOOLS: list[BaseTool] = [escalate_to_fazal]


class OrchestratorAgentState(AgentState, total=False):
    """State schema for the orchestrator ``create_agent`` subgraph (VT-3.4 PR 2/3).

    create_agent's default ``AgentState`` is messages-centric — its subgraph
    filters parent state down to that schema, so the supervisor's run-identity
    fields would never reach a handoff tool's ``InjectedState`` (verified seam,
    CL-209). This subclass adds them back, narrowly: extending ``AgentState``
    (rather than swapping the whole schema to ``AgentGraphState``) keeps
    create_agent's own state fields intact and keeps sales-recovery bundle
    fields OUT of the orchestrator subgraph.

    total=False: the three fields are populated by upstream producers
    (VT-3.3 / VT-3.5 / VT-3.8) and may be absent at orchestrator entry —
    matching ``AgentGraphState``'s totality for the same keys (CL-195).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_orchestrator_agent(
    model: ChatAnthropic,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the orchestrator-agent with the base tools plus ``extra_tools``.

    name="orchestrator_agent" is load-bearing — VT-3.4's supervisor graph
    references this exact string as the node name.

    ``state_schema=OrchestratorAgentState`` (VT-3.4 PR 2/3): propagates
    tenant_id / run_id / trigger_reason into the subgraph so the
    ``spawn_sales_recovery`` handoff can read them from ``InjectedState``.

    ``create_agent`` (langchain 1.x) is the supported successor to the
    deprecated ``langgraph.prebuilt.create_react_agent`` (CL-134).
    """
    return create_agent(
        model=model,
        tools=[*ORCHESTRATOR_AGENT_TOOLS, *extra_tools],
        system_prompt=ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
        name="orchestrator_agent",
        state_schema=OrchestratorAgentState,
    )


# Default module-level instance — base tools only. The VT-3.4 supervisor builds
# its own instance with the spawn_sales_recovery handoff tool added.
orchestrator_agent = build_orchestrator_agent(_MODEL)
