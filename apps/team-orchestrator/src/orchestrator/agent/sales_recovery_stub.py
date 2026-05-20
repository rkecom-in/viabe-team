"""Stub Sales Recovery Agent for VT-3.4 PR 1/3 (migrated to CampaignPlan v1.0 by VT-122).

Real implementation lands in VT-4. This stub exists only to exercise the
handoff seam: a no-tool agent whose system prompt instructs the LLM to emit a
hardcoded CampaignPlan as JSON. The graph node wrapping it parses the JSON and
falls back to a hardcoded plan on parse failure, keeping the happy-path test
deterministic.

Agent identity in the multi-agent graph comes from the parent StateGraph node
name (``graph.add_node("sales_recovery_agent", ...)``), so no ``name=`` is
needed on ``create_agent`` here.

The stub's hardcoded plan is the v1.0 ``proposed`` variant — the discriminated
union's only variant that produces a campaigns row. The supervisor node
overrides ``tenant_id`` + ``run_id`` from the run's state before persistence
so the placeholder identity fields below never reach the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic

from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanProposed,
    CampaignWindow,
    ConfidenceLevel,
    EvidenceRef,
    EvidenceSourceKind,
    ExpectedARRR,
    Language,
    MessagePlan,
    TargetCohort,
)

# Stub identity placeholders. supervisor.sales_recovery_node rewrites
# tenant_id + run_id from the live AgentGraphState before persistence
# (CL-202 / VT-3.4 PR 3/3) so these placeholders never reach a DB.
_STUB_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_STUB_RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
_STUB_CUSTOMER_ID = UUID("00000000-0000-0000-0000-000000000002")

_STUB_SYSTEM_PROMPT = """You are a stub Sales Recovery Agent.
Your only job is to acknowledge the handoff and produce a hardcoded
CampaignPlan v1.0 proposed-variant JSON. Do not reason about the
input. Reply with ONLY a single JSON object matching the v1.0
CampaignPlanProposed shape; no prose, no markdown fence.
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
    """Deterministic fallback for the happy-path test (v1.0 proposed variant).

    If the LLM's JSON emission is unparseable, the graph node falls back to
    this — so the test is deterministic on the seam (handoff fires, a
    CampaignPlan is returned) rather than on LLM output quality.

    Every required v1.0 ``proposed`` field is populated, validators pass:
      - cohort_size == len(customer_ids)
      - low_paise <= high_paise
      - evidence_refs non-empty, [E1] marker present in prose, claim_id E1 declared
      - campaign_window in the future
    """
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=_STUB_TENANT_ID,
        run_id=_STUB_RUN_ID,
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1),
            end=now + timedelta(days=7),
        ),
        target_cohort=TargetCohort(
            customer_ids=[_STUB_CUSTOMER_ID],
            cohort_label="stub-cohort",
            cohort_size=1,
            selection_reason="stub selection reason [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=0,
            high_paise=1,
            confidence=ConfidenceLevel.LOW,
            basis="stub basis [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="stub",
            ),
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "stub", "discount": "10"},
            language=Language.EN,
            personalization="stub-personalization",
        ),
    )
