"""Agent output schemas (VT-37).

The typed contracts the sales_recovery agent returns. Validators are
structural (Pillar 1) and enforce honesty (Pillar 7: confidence ranges
mandatory, evidence_refs mandatory, no unsupported prose claims).

Currently houses ``CampaignPlan`` only. Phase-2 specialists land their
own schemas here.
"""

from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanInsufficientData,
    CampaignPlanOutOfScope,
    CampaignPlanProposed,
    CampaignStatus,
    CampaignWindow,
    EscalationCondition,
    EvidenceRef,
    EvidenceSourceKind,
    ExpectedARRR,
    MessagePlan,
    MissingDataItem,
    SelfEvaluateStatus,
    SuggestedSpecialist,
    TargetCohort,
    parse_campaign_plan,
)

__all__ = [
    "CampaignPlan",
    "CampaignPlanInsufficientData",
    "CampaignPlanOutOfScope",
    "CampaignPlanProposed",
    "CampaignStatus",
    "CampaignWindow",
    "EscalationCondition",
    "EvidenceRef",
    "EvidenceSourceKind",
    "ExpectedARRR",
    "MessagePlan",
    "MissingDataItem",
    "SelfEvaluateStatus",
    "SuggestedSpecialist",
    "TargetCohort",
    "parse_campaign_plan",
]
