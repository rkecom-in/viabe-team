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

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import BaseTool, tool

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


def build_orchestrator_agent(
    model: ChatAnthropic,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the orchestrator-agent with the base tools plus ``extra_tools``.

    name="orchestrator_agent" is load-bearing — VT-3.4's supervisor graph
    references this exact string as the node name.

    ``create_agent`` (langchain 1.x) is the supported successor to the
    deprecated ``langgraph.prebuilt.create_react_agent`` (CL-134).
    """
    return create_agent(
        model=model,
        tools=[*ORCHESTRATOR_AGENT_TOOLS, *extra_tools],
        system_prompt=ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
        name="orchestrator_agent",
    )


# Default module-level instance — base tools only. The VT-3.4 supervisor builds
# its own instance with the spawn_sales_recovery handoff tool added.
orchestrator_agent = build_orchestrator_agent(_MODEL)
