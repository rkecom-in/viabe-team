"""Orchestrator-Agent skeleton (VT-3.9 PR 1/N).

The minimal orchestrator-agent: an Opus 4.7 react agent with the reviewed
system prompt and two placeholder tools. It exists so VT-3.4's supervisor has
an importable ``orchestrator_agent`` to wire — it is NOT yet called from
runner.py (the runner integration is VT-3.4 PR 1/3).

Deferred to later VT-3.9 PRs: L0 memory, the Context Composer bundle, the real
compose_owner_output / send_whatsapp_template / get_subscriber_state /
query_pipeline_history tools, and machine-enforced hard limits.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

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
def spawn_sales_recovery(context_summary: str, trigger_reason: str) -> str:
    """Hand off to Sales Recovery Agent for dormant-customer winback campaign work.

    Skeleton placeholder — this logs the handoff intent but does not yet invoke
    a specialist. VT-3.4 PR 1/3 replaces it with the real langgraph_supervisor
    handoff tool. Not for production paths.
    """
    logger.info(
        "spawn_sales_recovery (placeholder) trigger_reason=%s context_summary=%s",
        trigger_reason,
        context_summary,
    )
    return f"[placeholder] would spawn sales_recovery with: {trigger_reason}"


@tool
def escalate_to_fazal(run_id: str, reason: str, context: str) -> str:
    """Escalate to Fazal. Log-only in this skeleton; real wiring is VT-3.6."""
    logger.warning(
        "ESCALATE_TO_FAZAL run_id=%s reason=%s context=%s", run_id, reason, context
    )
    return f"[skeleton] escalation logged for run_id={run_id}"


ORCHESTRATOR_AGENT_TOOLS = [spawn_sales_recovery, escalate_to_fazal]

# name="orchestrator_agent" is load-bearing — VT-3.4's langgraph_supervisor
# wiring references this exact string. Do not change.
orchestrator_agent = create_react_agent(
    model=_MODEL,
    tools=ORCHESTRATOR_AGENT_TOOLS,
    prompt=ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
    name="orchestrator_agent",
)
