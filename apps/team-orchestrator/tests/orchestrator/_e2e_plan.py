"""VT-140 — deterministic CampaignPlan factory for the E2E harness (CI mode).

CI mode mocks the SR-agent's Anthropic call by returning a fixed PROPOSED plan
targeting the seeded cohort. This module builds a contract-valid v1.0
``CampaignPlanProposed`` so the real collapse → approval-gate → resume →
campaign_execute path runs unchanged against the synthetic tenant.

The template is ``team_weekly_approval`` (the only agent-selectable Phase-1
template with a real content_sid in both en + hi) so the gate's owner-approval
send + the per-recipient campaign send both resolve a real SID in mock mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from orchestrator.agent.schemas.campaign_plan import (
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

# Must match a registry template with a real content_sid + an agent_selectable
# flag (config/twilio_templates.yaml). Its variable signature is
# (customer_segment, campaign_mode, projected_recovery_inr).
E2E_TEMPLATE_ID = "team_weekly_approval"
E2E_TEMPLATE_PARAMS: dict[str, str] = {
    "customer_segment": "dormant_synthetic",
    "campaign_mode": "recovery",
    "projected_recovery_inr": "5000",
}


def build_proposed_plan(
    *,
    tenant_id: UUID,
    run_id: UUID,
    cohort_ids: list[UUID],
) -> CampaignPlanProposed:
    """A contract-valid PROPOSED CampaignPlan targeting ``cohort_ids``.

    Evidence-marker consistency (campaign_plan.py): every ``[E1]`` marker in a
    prose block must have a matching EvidenceRef and vice-versa. We cite [E1]
    in both selection_reason and basis and declare a single E1 ref.
    """
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=tenant_id,
        run_id=run_id,
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(minutes=5),
            end=now + timedelta(days=7),
        ),
        target_cohort=TargetCohort(
            customer_ids=list(cohort_ids),
            cohort_label="dormant_synthetic_cohort",
            cohort_size=len(cohort_ids),
            selection_reason=(
                "Synthetic dormant cohort seeded for the VT-140 E2E harness [E1]."
            ),
        ),
        expected_arrr=ExpectedARRR(
            low_paise=100_000,
            high_paise=500_000,
            confidence=ConfidenceLevel.MEDIUM,
            basis="Synthetic projection for the E2E harness [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="vt140-synthetic-evidence",
                note="Fabricated evidence for the synthetic harness (CL-422).",
            ),
        ],
        message_plan=MessagePlan(
            template_id=E2E_TEMPLATE_ID,
            template_params=dict(E2E_TEMPLATE_PARAMS),
            language=Language.EN,
            personalization="Synthetic personalization for the E2E harness.",
        ),
    )
