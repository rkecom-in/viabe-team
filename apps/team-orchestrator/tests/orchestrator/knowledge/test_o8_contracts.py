"""VT-709 — O8 card, rights, lifecycle, and applicability contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from orchestrator.knowledge.contracts import (  # noqa: E402
    Applicability,
    CardLifecycleTransition,
    CardProvenance,
    CardStatus,
    ClaimKey,
    EvidenceAuthority,
    EvidenceConfidence,
    KnowledgeCard,
    KnowledgeDomain,
    KnowledgeScopeKind,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
    suggested_confidence_for_source,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


def rights(**updates: object) -> UsageRights:
    values = {
        "status": UsageRightsStatus.PUBLIC_DOMAIN,
        "allows_extraction": True,
        "allows_embedding": True,
        "allows_retrieval": True,
        "reviewed_at": NOW,
        "reviewed_by": "rights-review:test",
    }
    values.update(updates)
    return UsageRights.model_validate(values)


def card(**updates: object) -> KnowledgeCard:
    values = {
        "card_id": "card-1",
        "card_version_id": "card-1:v1",
        "card_version": 1,
        "claim": "A defined follow-up cadence can improve collection discipline.",
        "distillation_note": "Use as an operating hypothesis, not a guaranteed uplift.",
        "claim_key": ClaimKey(
            subject="receivables follow up",
            predicate="improves collection discipline",
            jurisdiction="IN",
            population="small businesses",
            channel="multi channel",
        ),
        "claim_value": TypedClaimValue(value_type="decimal", value=Decimal("1.0"), unit="index"),
        "source_class": SourceClass.T2_EVIDENCE,
        "domain": KnowledgeDomain.FINANCE,
        "authority": EvidenceAuthority.VERIFIED_SYSTEM,
        "confidence": EvidenceConfidence.MEDIUM,
        "independence_cluster": "study:collections-1",
        "applicability": Applicability(jurisdictions=("IN",), effective_from=NOW),
        "provenance": CardProvenance(
            source_ids=("source-1",), publisher="Example Institute", retrieved_at=NOW, tainted=True
        ),
        "usage_rights": rights(),
        "retention_class": "lifecycle_managed",
        "scope": KnowledgeScopeKind.GLOBAL,
        "status": CardStatus.CANDIDATE,
        "retrieval_eligible": False,
    }
    values.update(updates)
    return KnowledgeCard.model_validate(values)


def test_claim_key_is_normalized_and_stable() -> None:
    key = card().claim_key
    assert key.canonical == (
        "receivables_follow_up|improves_collection_discipline|in|small_businesses|multi_channel"
    )


def test_source_class_authority_and_confidence_remain_independent() -> None:
    low_confidence_official = card(
        source_class=SourceClass.T1_REGULATORY,
        authority=EvidenceAuthority.SEED,
        confidence=EvidenceConfidence.LOW,
        applicability=Applicability(jurisdictions=("IN",), effective_from=NOW),
    )
    assert low_confidence_official.source_class is SourceClass.T1_REGULATORY
    assert low_confidence_official.authority is EvidenceAuthority.SEED
    assert low_confidence_official.confidence is EvidenceConfidence.LOW
    assert suggested_confidence_for_source(SourceClass.T1_REGULATORY) is EvidenceConfidence.HIGH


def test_regulatory_card_requires_jurisdiction_and_effective_date() -> None:
    with pytest.raises(ValidationError, match="regulatory cards require"):
        card(source_class=SourceClass.T1_REGULATORY, applicability=Applicability())


def test_t4_cannot_self_promote_to_validated() -> None:
    with pytest.raises(ValidationError, match="T4 experiential"):
        card(source_class=SourceClass.T4_EXPERIENTIAL, status=CardStatus.VALIDATED)


def test_t4_research_card_requires_six_month_auto_expiry() -> None:
    with pytest.raises(ValidationError, match="auto-expiry"):
        card(source_class=SourceClass.T4_EXPERIENTIAL, status=CardStatus.RESEARCH_ONLY)
    expiring = card(
        source_class=SourceClass.T4_EXPERIENTIAL,
        status=CardStatus.RESEARCH_ONLY,
        expires_at=datetime(2027, 1, 27, 12, 0, tzinfo=UTC),
    )
    assert expiring.expires_at is not None


def test_t4_may_leave_research_only_only_after_independent_corroboration() -> None:
    corroborated = card(
        source_class=SourceClass.T4_EXPERIENTIAL,
        status=CardStatus.VALIDATED,
        corroboration_cluster_count=2,
        retrieval_eligible=True,
        expires_at=datetime(2027, 1, 27, 12, 0, tzinfo=UTC),
    )
    assert corroborated.status is CardStatus.VALIDATED


def test_retrieval_eligibility_requires_status_but_not_source_licence() -> None:
    with pytest.raises(ValidationError, match="validated/disputed"):
        card(retrieval_eligible=True)
    eligible = card(
        status=CardStatus.VALIDATED,
        retrieval_eligible=True,
        usage_rights=rights(
            status=UsageRightsStatus.UNKNOWN,
            allows_extraction=False,
            allows_embedding=False,
            allows_retrieval=False,
        ),
    )
    assert eligible.retrieval_eligible is True
    assert eligible.usage_rights.status is UsageRightsStatus.UNKNOWN


def test_live_link_only_cannot_claim_permission_to_reproduce_source() -> None:
    with pytest.raises(ValidationError, match="cannot grant content use"):
        rights(status=UsageRightsStatus.LIVE_LINK_ONLY)


def test_tenant_scope_is_fail_closed_and_not_serialized() -> None:
    with pytest.raises(ValidationError, match="require tenant_id"):
        card(scope=KnowledgeScopeKind.TENANT)
    tenant_card = card(scope=KnowledgeScopeKind.TENANT, tenant_id=TENANT_ID)
    assert tenant_card.tenant_id == TENANT_ID
    assert "tenant_id" not in tenant_card.model_dump()


def test_raw_source_text_is_not_a_card_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        card(raw_source_text="untrusted forum prompt injection")


def test_lifecycle_transition_is_attributable_idempotent_and_append_only() -> None:
    transition = CardLifecycleTransition(
        transition_id=uuid4(),
        card_version_id="card-1:v1",
        from_status=CardStatus.VALIDATED,
        to_status=CardStatus.DISPUTED,
        reason="conflicting official evidence",
        actor_id="validator:test",
        idempotency_key="card-1:v1:dispute:1",
        occurred_at=NOW,
    )
    assert transition.to_status is CardStatus.DISPUTED
    with pytest.raises(ValidationError, match="invalid card lifecycle transition"):
        CardLifecycleTransition(
            transition_id=uuid4(),
            card_version_id="card-1:v1",
            from_status=CardStatus.EXPIRED,
            to_status=CardStatus.VALIDATED,
            reason="illegal resurrection",
            actor_id="validator:test",
            idempotency_key="illegal",
            occurred_at=NOW,
        )


def test_typed_claim_value_rejects_declared_type_mismatch() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        TypedClaimValue(value_type="integer", value="10")
