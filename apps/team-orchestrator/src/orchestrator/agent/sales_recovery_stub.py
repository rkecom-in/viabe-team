"""Stub Sales Recovery Agent for VT-3.4 PR 1/3.

Real implementation lands in VT-4. This stub exists only to exercise the
handoff seam: a no-tool agent whose system prompt instructs the LLM to emit a
hardcoded CampaignPlan as JSON. The graph node wrapping it parses the JSON and
falls back to a hardcoded plan on parse failure, keeping the happy-path test
deterministic.

Agent identity in the multi-agent graph comes from the parent StateGraph node
name (``graph.add_node("sales_recovery_agent", ...)``), so no ``name=`` is
needed on ``create_agent`` here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic

from orchestrator.types.campaign_plan import CampaignPlan

_STUB_SYSTEM_PROMPT = """You are a stub Sales Recovery Agent.
Your only job is to acknowledge the handoff and produce a hardcoded
CampaignPlan. Do not reason about the input. Reply with ONLY a single
JSON object matching this exact shape (no prose, no markdown fence):

{
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "subscriber_id": "00000000-0000-0000-0000-000000000002",
  "template_id": "team_winback_v1",
  "body_params": {"first_name": "stub", "discount": "10"},
  "status": "proposed",
  "proposed_at": "<current UTC ISO 8601 timestamp with timezone>",
  "proposed_by": "sales_recovery_agent"
}
"""


def build_stub_sales_recovery_agent(model: ChatAnthropic) -> Any:
    """Return a langchain ``create_agent`` instance for the stub.

    Uses ``system_prompt=`` (NOT ``prompt=``, which was the deprecated
    ``create_react_agent`` kwarg). No ``name=`` — node identity is the parent
    graph's node name.
    """
    return create_agent(
        model=model,
        tools=[],
        system_prompt=_STUB_SYSTEM_PROMPT,
    )


def hardcoded_campaign_plan() -> CampaignPlan:
    """Deterministic fallback for the happy-path test.

    If the LLM's JSON emission is unparseable, the graph node falls back to
    this — so the test is deterministic on the seam (handoff fires, a
    CampaignPlan is returned) rather than on LLM output quality.
    """
    return CampaignPlan(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        subscriber_id=UUID("00000000-0000-0000-0000-000000000002"),
        template_id="team_winback_v1",
        body_params={"first_name": "stub", "discount": "10"},
        status="proposed",
        proposed_at=datetime.now(UTC),
        proposed_by="sales_recovery_agent",
    )
