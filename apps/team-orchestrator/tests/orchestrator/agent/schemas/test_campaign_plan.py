"""VT-37 — CampaignPlan structured-output schema tests.

Three surfaces:

1. Variant happy paths — each of the three ``status`` values builds, round-trips
   through model_validate / model_dump, and surfaces as the right concrete class
   via the discriminator.
2. Discriminator rejection — a payload's variant-foreign fields are caught
   (e.g. ``out_of_scope`` carrying a ``campaign_window``).
3. Validator suite — every named validator on the proposed variant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.agent.schemas.campaign_plan import (  # noqa: E402
    CampaignPlan,
    CampaignPlanInsufficientData,
    CampaignPlanOutOfScope,
    CampaignPlanProposed,
    CampaignStatus,
    CampaignWindow,
    ConfidenceLevel,
    EscalationCondition,
    EvidenceRef,
    EvidenceSourceKind,
    ExpectedARRR,
    Language,
    MessagePlan,
    MissingDataItem,
    SelfEvaluateStatus,
    SuggestedSpecialist,
    TargetCohort,
    parse_campaign_plan,
)


# ---------- fixtures ----------------------------------------------------------


def _future(days: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _customer_ids(n: int = 2) -> list:
    return [uuid4() for _ in range(n)]


def _proposed_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build a valid CampaignPlanProposed kwargs dict. Overrideable."""
    cids = _customer_ids(2)
    kwargs: dict[str, Any] = dict(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        campaign_window=CampaignWindow(
            start=_future(1),
            end=_future(7),
        ),
        target_cohort=TargetCohort(
            customer_ids=cids,
            cohort_label="60-90 day dormants",
            cohort_size=len(cids),
            selection_reason=(
                "Dormant customers from the last quarter [E1] with above-"
                "average historical ARPU [E2]."
            ),
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00,
            high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM,
            basis="Based on prior winback yields [E1] and current ARPU [E2].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="query_pipeline_history:abc",
            ),
            EvidenceRef(
                claim_id="E2",
                source_kind=EvidenceSourceKind.L4_SKILL_CORPUS,
                source_id="winback_yields_q3",
            ),
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner", "discount": "10"},
            language=Language.EN,
            personalization="Owner-first-name personalisation.",
        ),
        escalation_conditions=[
            EscalationCondition(trigger="cohort_size > 500", threshold=500),
        ],
    )
    kwargs.update(overrides)
    return kwargs


# ---------- 1. Variant happy paths -------------------------------------------


def test_proposed_variant_builds_and_round_trips():
    plan = CampaignPlanProposed(**_proposed_kwargs())
    dumped = plan.model_dump()
    assert dumped["status"] == CampaignStatus.PROPOSED.value
    parsed = parse_campaign_plan(dumped)
    assert isinstance(parsed, CampaignPlanProposed)
    assert parsed == plan


def test_out_of_scope_variant_builds_and_round_trips():
    plan = CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        out_of_scope_reason="Owner asked about reputation management, not sales recovery.",
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )
    parsed = parse_campaign_plan(plan.model_dump())
    assert isinstance(parsed, CampaignPlanOutOfScope)
    assert parsed == plan
    assert parsed.suggested_specialist is SuggestedSpecialist.REPUTATION


def test_insufficient_data_variant_builds_and_round_trips():
    plan = CampaignPlanInsufficientData(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        missing_data=[
            MissingDataItem(
                category="customer_ledger",
                description="customer_ledger has 0 rows",
                suggested_remediation="prompt owner to onboard via paper-book ingestion",
            ),
        ],
    )
    parsed = parse_campaign_plan(plan.model_dump())
    assert isinstance(parsed, CampaignPlanInsufficientData)
    assert parsed == plan


def test_self_evaluate_status_defaults_to_not_yet_evaluated():
    """VT-37 does NOT enforce the evaluation gate (VT-4.5 owns that). The
    default value distinguishes a freshly-emitted draft from an evaluated
    one."""
    plan = CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        out_of_scope_reason="Out of scope.",
    )
    assert plan.self_evaluate_status is SelfEvaluateStatus.NOT_YET_EVALUATED


# ---------- 2. Discriminator rejection ---------------------------------------


def test_discriminator_rejects_out_of_scope_with_campaign_window():
    """An out_of_scope payload carrying a proposed-variant field
    (campaign_window) must be rejected — pydantic discriminated unions
    have ``extra='forbid'`` semantics per-variant."""
    payload = {
        "version": "1.0",
        "status": "out_of_scope",
        "tenant_id": str(uuid4()),
        "run_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "out_of_scope_reason": "Unsupported request.",
        "campaign_window": {  # variant-foreign
            "start": _future(1).isoformat(),
            "end": _future(7).isoformat(),
        },
    }
    with pytest.raises(Exception):  # pydantic.ValidationError
        parse_campaign_plan(payload)


def test_discriminator_rejects_insufficient_data_with_expected_arrr():
    """Same protection for the insufficient_data variant."""
    payload = {
        "version": "1.0",
        "status": "insufficient_data",
        "tenant_id": str(uuid4()),
        "run_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "missing_data": [
            {"category": "x", "description": "y", "suggested_remediation": "z"},
        ],
        "expected_arrr": {  # variant-foreign
            "low_paise": 0,
            "high_paise": 100,
            "confidence": "low",
            "basis": "spurious",
        },
    }
    with pytest.raises(Exception):
        parse_campaign_plan(payload)


def test_unknown_status_rejected():
    payload = {
        "version": "1.0",
        "status": "approved",  # lifecycle state, NOT on this contract
        "tenant_id": str(uuid4()),
        "run_id": str(uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(Exception):
        parse_campaign_plan(payload)


# ---------- 3. Validators on proposed variant --------------------------------


def test_campaign_window_end_must_be_greater_than_start():
    with pytest.raises(Exception, match="must be > start"):
        CampaignWindow(start=_future(7), end=_future(1))


def test_campaign_window_start_must_not_be_in_the_past():
    with pytest.raises(Exception, match="in the past"):
        CampaignWindow(
            start=datetime.now(UTC) - timedelta(hours=1),
            end=_future(1),
        )


def test_campaign_window_timestamps_must_be_tz_aware():
    naive_now = datetime.utcnow()
    with pytest.raises(Exception, match="timezone-aware"):
        CampaignWindow(start=naive_now, end=naive_now + timedelta(days=1))


def test_cohort_size_must_equal_customer_id_count():
    cids = _customer_ids(3)
    with pytest.raises(Exception, match="cohort_size"):
        TargetCohort(
            customer_ids=cids,
            cohort_label="label",
            cohort_size=2,  # mismatch
            selection_reason="reason",
        )


def test_expected_arrr_low_must_not_exceed_high():
    with pytest.raises(Exception, match="low_paise"):
        ExpectedARRR(
            low_paise=100,
            high_paise=50,
            confidence=ConfidenceLevel.LOW,
            basis="reason",
        )


def test_expected_arrr_rejects_negative_paise():
    with pytest.raises(Exception):
        ExpectedARRR(
            low_paise=-1,
            high_paise=10,
            confidence=ConfidenceLevel.LOW,
            basis="reason",
        )


def test_evidence_refs_must_be_non_empty_on_proposed():
    with pytest.raises(Exception):
        CampaignPlanProposed(**_proposed_kwargs(evidence_refs=[]))


def test_prose_marker_without_matching_claim_rejected():
    """Prose contains [E3] but no evidence_ref has claim_id='E3' — reject."""
    cids = _customer_ids(2)
    kwargs = _proposed_kwargs()
    kwargs["target_cohort"] = TargetCohort(
        customer_ids=cids,
        cohort_label="label",
        cohort_size=len(cids),
        selection_reason="claim [E3] without matching evidence_ref",
    )
    with pytest.raises(Exception, match="without backing evidence_refs"):
        CampaignPlanProposed(**kwargs)


def test_evidence_ref_without_matching_prose_marker_rejected():
    """evidence_ref(claim_id='E9') exists but no prose contains [E9] — reject."""
    kwargs = _proposed_kwargs()
    kwargs["evidence_refs"] = list(kwargs["evidence_refs"]) + [
        EvidenceRef(
            claim_id="E9",
            source_kind=EvidenceSourceKind.TOOL_CALL,
            source_id="uncited",
        ),
    ]
    with pytest.raises(Exception, match="not cited by any prose marker"):
        CampaignPlanProposed(**kwargs)


def test_evidence_ref_claim_id_must_match_E_pattern():
    """claim_id must be of the form ``E<digit+>`` — guards the marker
    regex from being defeated by an off-pattern id."""
    with pytest.raises(Exception):
        EvidenceRef(
            claim_id="not-an-E-id",
            source_kind=EvidenceSourceKind.TOOL_CALL,
            source_id="x",
        )


def test_proposed_exclusion_orphan_reason_rejected():
    """An exclusion_reasons key not present in exclusion_list is an orphan."""
    kwargs = _proposed_kwargs()
    orphan_cid = uuid4()
    kwargs["exclusion_list"] = []
    kwargs["exclusion_reasons"] = {orphan_cid: "opted out"}
    with pytest.raises(Exception, match="not in exclusion_list"):
        CampaignPlanProposed(**kwargs)


def test_proposed_exclusion_missing_reason_rejected():
    """An exclusion_list entry without a reason is invalid."""
    kwargs = _proposed_kwargs()
    cid = uuid4()
    kwargs["exclusion_list"] = [cid]
    kwargs["exclusion_reasons"] = {}
    with pytest.raises(Exception, match="missing reasons"):
        CampaignPlanProposed(**kwargs)


def test_proposed_has_no_lifecycle_fields():
    """CampaignPlan.status carries ONLY agent-terminal states.
    Lifecycle states (approved/rejected/sent/failed) are downstream.
    Lock against any future addition that conflates the two."""
    legal = {s.value for s in CampaignStatus}
    forbidden = {"approved", "rejected", "sent", "failed"}
    assert legal.isdisjoint(forbidden), (
        f"lifecycle states must not be on agent contract: "
        f"{legal & forbidden}"
    )


def test_top_level_campaignplan_alias_works_as_discriminated_union():
    """Sanity: parse_campaign_plan returns concrete variants based on status."""
    cases = [
        (
            CampaignPlanProposed(**_proposed_kwargs()).model_dump(),
            CampaignPlanProposed,
        ),
        (
            CampaignPlanOutOfScope(
                tenant_id=uuid4(),
                run_id=uuid4(),
                generated_at=datetime.now(UTC),
                out_of_scope_reason="x",
            ).model_dump(),
            CampaignPlanOutOfScope,
        ),
        (
            CampaignPlanInsufficientData(
                tenant_id=uuid4(),
                run_id=uuid4(),
                generated_at=datetime.now(UTC),
                missing_data=[
                    MissingDataItem(
                        category="a", description="b", suggested_remediation="c"
                    )
                ],
            ).model_dump(),
            CampaignPlanInsufficientData,
        ),
    ]
    for payload, expected_cls in cases:
        parsed = parse_campaign_plan(payload)
        assert isinstance(parsed, expected_cls)


# Type-alias for static-type assertions; mypy users importing CampaignPlan
# should see a discriminated union, not a concrete class.
_T: CampaignPlan = CampaignPlanProposed(**_proposed_kwargs())  # type: ignore[assignment]
