"""VT-3.4 PR 1/3 — happy-path test for the supervisor handoff.

Per CL-129: PR 1/3 ships ONE test. PRs 2/3 and 3/3 add the others.

This test makes two real Anthropic calls (the orchestrator's spawn decision +
the stub's JSON emission), so it is @pytest.mark.integration — skipped unless
RUN_INTEGRATION_TESTS=1 (the conftest gate) and additionally guarded on
ANTHROPIC_API_KEY, matching the VT-3.9 test convention.

Module-level imports run after the importorskip guards, so collecting this
file in the CI ``orchestrator`` job import-checks the whole supervisor chain
even though the test body itself is integration-gated.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_anthropic import ChatAnthropic  # noqa: E402 — after importorskip

from orchestrator.supervisor import build_supervisor_graph  # noqa: E402
from orchestrator.types.campaign_plan import CampaignPlan  # noqa: E402


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_orchestrator_spawns_sales_recovery_returns_campaign_plan():
    """Orchestrator routes to the stub specialist; the specialist returns a
    CampaignPlan.

    Asserts: the graph runs end-to-end; active_agent == 'sales_recovery_agent';
    campaign_plan is a valid CampaignPlan with proposed_by/status as expected.
    """
    model = ChatAnthropic(model="claude-opus-4-7")  # type: ignore[call-arg]
    graph = build_supervisor_graph(model=model)

    result = graph.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Recover dormant customers from the last 60 days",
                }
            ]
        }
    )

    assert result.get("active_agent") == "sales_recovery_agent"
    plan = result.get("campaign_plan")
    assert isinstance(plan, CampaignPlan)
    assert plan.proposed_by == "sales_recovery_agent"
    assert plan.status == "proposed"
