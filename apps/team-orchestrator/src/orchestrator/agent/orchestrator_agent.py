"""Orchestrator-Agent — the supervisor of the multi-agent graph (VT-3.9 / VT-3.4 / VT-125).

An Opus 4.7 agent built with langchain ``create_agent`` and the reviewed system
prompt. ``build_orchestrator_agent`` is the factory: callers pass ``extra_tools``
to add context-specific tools (VT-3.4's supervisor passes the ``spawn_sales_recovery``
handoff tool — a specialist handoff is only meaningful inside the parent graph,
so it is NOT in the base tool set).

The module-level ``orchestrator_agent`` is the default-built instance (base tools
only); it is the importable handle used by tests and any standalone caller.

VT-125 (this row) registers the broader tool inventory: in-scope tools that
exist on main (``compose_owner_output_tool``, ``self_evaluate``) plus
explicit STUB tools for L0 memory / send-whatsapp / subscriber-state /
pipeline-history. The stubs log intent + return placeholder outputs;
their real wiring lands in successor VT-N rows (tagged in each stub's
docstring).

Hard-limit enforcement (5 tool calls / 10K tokens / depth 3 / 2-min /
₹5) lives in the companion `orchestrator_agent_driver.py` (VT-125).
The agent itself is a langchain `create_agent` runnable — the driver
wraps invocation with usage tracking + `HardLimitExceeded` raising.

Observability: orchestrator-agent uses langchain's ``ChatAnthropic`` (NOT
direct ``client.messages.create``), so VT-182's ``@with_reasoning_capture``
decorator does not apply. VT-125 adds ``OrchestratorReasoningCallback`` —
a ``langchain_core.callbacks.BaseCallbackHandler`` that fires on
``on_llm_end`` and calls ``write_step('agent_reasoning_step', ...)``
under the active ``ObservabilityContext`` (VT-181 ContextVar). Callers
attach the callback per invocation via the driver.
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

from orchestrator.agent.tools.compose_output import compose_owner_output_tool
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


# ---------------------------------------------------------------------------
# Base tools — VT-125 inventory (existing real tools + STUBs for not-yet-shipped).
#
# Stub tools log intent + return placeholder. Marked in docstrings with the
# successor VT-N row that ships the real wiring. The orchestrator-agent's
# system prompt enumerates these so the model knows the surface area.
# ---------------------------------------------------------------------------


@tool
def escalate_to_fazal(run_id: str, reason: str, context: str) -> str:
    """Escalate to Fazal. Log-only in this skeleton; real wiring is VT-3.6."""
    logger.warning(
        "ESCALATE_TO_FAZAL run_id=%s reason=%s context=%s",
        run_id, reason, context,
    )
    return f"[skeleton] escalation logged for run_id={run_id}"


@tool
def write_l0_fragment_stub(tenant_id: str, content: str, tags: list[str]) -> str:
    """STUB — append an L0 memory fragment.

    TODO(VT-126): wire real L0 memory write. Today this logs the intent
    and returns a placeholder ack so the orchestrator-agent can express
    memory-write intent without crashing.
    """
    logger.info(
        "[VT-126 STUB] write_l0_fragment tenant_id=%s tags=%s content_len=%d",
        tenant_id, tags, len(content),
    )
    return "[stub] fragment intent logged; real write deferred to VT-126"


@tool
def query_l0_stub(tenant_id: str, query: str, k: int = 5) -> list[str]:
    """STUB — query L0 memory.

    TODO(VT-126): wire real L0 recall. Today returns an empty list so
    the orchestrator-agent can express memory-read intent.
    """
    logger.info(
        "[VT-126 STUB] query_l0 tenant_id=%s k=%d query=%r",
        tenant_id, k, query,
    )
    return []


@tool
def send_whatsapp_template_stub(
    tenant_id: str, template_name: str, variables: dict[str, str]
) -> str:
    """STUB — send a Twilio Content API template message.

    TODO(VT-5.7): wire real Twilio send. Today logs the intent and
    returns a placeholder SID so the orchestrator-agent can express
    send intent.
    """
    logger.info(
        "[VT-5.7 STUB] send_whatsapp_template tenant_id=%s template=%s vars=%s",
        tenant_id, template_name, variables,
    )
    return f"[stub] template send intent logged: {template_name}"


@tool
def get_subscriber_state_stub(tenant_id: str) -> dict[str, str]:
    """STUB — fetch subscriber state.

    TODO(VT-5.2): wire real read against ``subscriber_states`` table.
    Today returns a minimal placeholder.
    """
    logger.info("[VT-5.2 STUB] get_subscriber_state tenant_id=%s", tenant_id)
    return {"tenant_id": tenant_id, "phase": "unknown", "stub": "true"}


@tool
def query_pipeline_history_stub(
    tenant_id: str, lookback_hours: int = 24
) -> list[dict[str, str]]:
    """STUB — query recent pipeline_runs for a tenant.

    TODO(VT-5.3): wire real SELECT against pipeline_runs + pipeline_steps.
    Today returns an empty list.
    """
    logger.info(
        "[VT-5.3 STUB] query_pipeline_history tenant_id=%s lookback_hours=%d",
        tenant_id, lookback_hours,
    )
    return []


# Base tools every orchestrator-agent has, regardless of context. Specialist
# handoff tools (spawn_*) are NOT here — they are passed as extra_tools by the
# supervisor graph, since a handoff is only valid inside the parent graph.
#
# VT-125 inventory: real tools (compose_owner_output_tool) + 5 STUBs marked
# in each tool's docstring with the successor VT-N row.
# self_evaluate is OMITTED from the base inventory — its MCPTool subclass
# signature mismatches langchain's @tool surface (VT-181 retrofit deferred);
# specialist agents invoke it directly via the MCPTool framework.
ORCHESTRATOR_AGENT_TOOLS: list[BaseTool] = [
    escalate_to_fazal,
    compose_owner_output_tool,
    write_l0_fragment_stub,
    query_l0_stub,
    send_whatsapp_template_stub,
    get_subscriber_state_stub,
    query_pipeline_history_stub,
]


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

    Per VT-125: caller wraps invocation with ``OrchestratorAgentDriver``
    for hard-limit enforcement + ``OrchestratorReasoningCallback`` for
    observability. The agent itself is a plain runnable.
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
