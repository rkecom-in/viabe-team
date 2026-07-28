"""VT-711 learning-loop, contribution-control and differencing tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.learning_loop import (  # noqa: E402
    DistilledLesson,
    InMemoryGeneralCandidateSink,
    InMemoryPriorCandidateSink,
    InMemoryPriorContributionStore,
    InMemoryTenantMemorySink,
    KAnonDecision,
    LearningLoop,
    LearningRejected,
    LearningScope,
    OutcomeAttribution,
    PriorAdmissionPolicy,
    PriorBuilder,
    PriorContribution,
    PriorDisposition,
    PriorKey,
    ScopeTriageDecision,
    TriageAuthority,
)
from orchestrator.knowledge_contracts import KnowledgeLayer  # noqa: E402

_NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
_OLD = _NOW - timedelta(days=45)


def _key() -> PriorKey:
    return PriorKey(
        subject="sales_follow_up",
        predicate="customer_replied",
        jurisdiction="india",
        business_archetype="retail",
        size_band="micro",
        maturity_stage="established",
        channel="whatsapp",
    )


def _policy(**updates) -> PriorAdmissionPolicy:
    values = {
        "k_min": 10,
        "differencing_buffer_tenants": 1,
        "max_contributions_per_tenant": 1,
        "max_tenant_share": 0.1,
        "minimum_window_tenants": 5,
        "max_window_mean_delta": 0.2,
        "max_leave_one_tenant_out_delta": 0.05,
        "quarantine_days": 30,
        "published_decimal_places": 3,
    }
    values.update(updates)
    return PriorAdmissionPolicy(**values)


def _gate(tenant_ids: set[UUID], cohort_key: str, k_min: int) -> KAnonDecision:
    assert cohort_key == _key().canonical
    return KAnonDecision(
        admitted=len(tenant_ids) >= k_min,
        tenant_count=len(tenant_ids),
        reason="admitted" if len(tenant_ids) >= k_min else "below_k_min",
    )


def _contribution(
    index: int,
    *,
    tenant_id: UUID | None = None,
    value: float = 0.5,
    captured_at: datetime = _OLD,
) -> PriorContribution:
    return PriorContribution(
        contribution_id=f"contribution-{index}",
        tenant_id=tenant_id or UUID(int=index + 1),
        key=_key(),
        normalized_outcome=value,
        occurred_at=captured_at - timedelta(hours=1),
        captured_at=captured_at,
        evidence_ref_digest=f"{index + 1:064x}",
    )


def test_exact_k_is_rejected_when_differencing_would_drop_below_k() -> None:
    result = PriorBuilder(_policy(), k_gate=_gate).assess(
        [_contribution(index) for index in range(10)], now=_NOW
    )
    assert result.disposition is PriorDisposition.REJECTED
    assert "differencing_buffer_not_met" in result.reasons
    assert result.candidate is None


def test_k_plus_buffer_stable_prior_becomes_anonymized_candidate() -> None:
    contributions = [_contribution(index) for index in range(11)]
    result = PriorBuilder(_policy(), k_gate=_gate).assess(contributions, now=_NOW)
    assert result.disposition is PriorDisposition.CANDIDATE
    assert result.candidate is not None
    assert result.candidate.aggregate_mean == 0.5
    assert result.candidate.contributor_band == "up_to_20"
    rendered = result.candidate.model_dump_json()
    for item in contributions:
        assert str(item.tenant_id) not in rendered
        assert item.contribution_id not in rendered
    assert "lesson_input" not in rendered
    assert "evidence_ref" not in rendered


def test_prior_remains_quarantined_until_full_period_elapses() -> None:
    contributions = [_contribution(index, captured_at=_NOW) for index in range(11)]
    result = PriorBuilder(_policy(), k_gate=_gate).assess(contributions, now=_NOW)
    assert result.disposition is PriorDisposition.QUARANTINED
    assert result.candidate is not None
    assert result.candidate.retrieval_eligible is False


def test_per_tenant_cap_and_share_prevent_one_tenant_dominance() -> None:
    dominant = uuid4()
    contributions = [_contribution(index, tenant_id=dominant) for index in range(20)]
    contributions.extend(_contribution(100 + index) for index in range(10))
    result = PriorBuilder(_policy(), k_gate=_gate).assess(contributions, now=_NOW)
    assert result.capped_contribution_count == 11
    assert result.distinct_tenant_count == 11
    assert result.disposition is PriorDisposition.CANDIDATE


def test_temporal_instability_rejects_prior() -> None:
    contributions = [
        _contribution(index, value=0.0 if index < 5 else 1.0) for index in range(11)
    ]
    result = PriorBuilder(_policy(max_window_mean_delta=0.1), k_gate=_gate).assess(
        contributions, now=_NOW
    )
    assert result.disposition is PriorDisposition.REJECTED
    assert "stability_delta_exceeded" in result.reasons


def test_k_gate_count_disagreement_fails_closed() -> None:
    def lying_gate(tenant_ids: set[UUID], cohort_key: str, k_min: int) -> KAnonDecision:
        del tenant_ids, cohort_key, k_min
        return KAnonDecision(admitted=True, tenant_count=999, reason="admitted")

    with pytest.raises(LearningRejected, match="count disagrees"):
        PriorBuilder(_policy(), k_gate=lying_gate).assess(
            [_contribution(index) for index in range(11)], now=_NOW
        )


def test_contribution_share_ceiling_tightens_when_k_is_raised() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        _policy(k_min=20, max_tenant_share=0.1)


class _Distiller:
    tools_enabled = False

    def __init__(self, statement: str = "A structured follow-up improved response quality") -> None:
        self.statement = statement

    def distill(self, attribution: OutcomeAttribution) -> DistilledLesson:
        del attribution
        return DistilledLesson(
            abstract_statement=self.statement,
            mechanism_code="structured_follow_up",
        )


def _attribution(
    tenant_id: UUID,
    *,
    repeatable: bool = False,
    lesson_input: str = "Tenant-specific raw situation",
) -> OutcomeAttribution:
    return OutcomeAttribution(
        tenant_id=tenant_id,
        outcome_ref=f"outcome-{tenant_id.hex[:8]}",
        occurred_at=_OLD,
        evidence_refs=(f"tenant-evidence:{tenant_id}",),
        lesson_input=lesson_input,
        prior_key=_key() if repeatable else None,
        normalized_outcome=0.5 if repeatable else None,
    )


def _loop(distiller: _Distiller, *, policy: PriorAdmissionPolicy | None = None):
    tenant = InMemoryTenantMemorySink()
    contributions = InMemoryPriorContributionStore()
    general = InMemoryGeneralCandidateSink()
    priors = InMemoryPriorCandidateSink()
    loop = LearningLoop(
        distiller=distiller,
        tenant_memory=tenant,
        contributions=contributions,
        general_candidates=general,
        prior_candidates=priors,
        prior_builder=PriorBuilder(policy or _policy(), k_gate=_gate),
    )
    return loop, tenant, contributions, general, priors


def _triage(scope: LearningScope, *, layer: KnowledgeLayer | None = None) -> ScopeTriageDecision:
    return ScopeTriageDecision(
        scope=scope,
        authority=TriageAuthority.DETERMINISTIC_POLICY,
        decision_ref="policy:vt711-test",
        decided_at=_NOW,
        tenant_layer=layer,
    )


def test_tenant_lesson_routes_only_to_l1_l2() -> None:
    loop, tenant, _, general, priors = _loop(_Distiller())
    tenant_id = uuid4()
    result = loop.process(
        _attribution(tenant_id), _triage(LearningScope.TENANT, layer=KnowledgeLayer.L2), now=_NOW
    )
    assert result.destination == "l2"
    assert len(tenant.records) == 1
    assert tenant.records[0].tenant_id == tenant_id
    assert general.candidates == []
    assert priors.candidates == []


def test_general_lesson_is_candidate_only_and_tenant_refs_are_digested() -> None:
    loop, tenant, _, general, priors = _loop(_Distiller())
    tenant_id = uuid4()
    result = loop.process(
        _attribution(tenant_id),
        _triage(LearningScope.GENERAL),
        now=_NOW,
        name_registry=lambda _: False,
    )
    assert result.destination == "candidate_registry"
    assert tenant.records == [] and priors.candidates == []
    candidate = general.candidates[0]
    assert candidate.status == "candidate"
    assert candidate.retrieval_eligible is False
    rendered = candidate.model_dump_json()
    assert str(tenant_id) not in rendered
    assert "tenant-evidence" not in rendered
    assert candidate.evidence_refs[0].startswith("sha256:")


def test_global_path_rejects_raw_story_and_unique_redaction_token(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt711-test")
    tenant_id = uuid4()
    loop, *_ = _loop(_Distiller("Call customer at +919876543210"))
    with pytest.raises(LearningRejected, match="redaction token"):
        loop.process(
            _attribution(tenant_id),
            _triage(LearningScope.GENERAL),
            now=_NOW,
            name_registry=lambda _: False,
        )

    class _RawStoryDistiller(_Distiller):
        def distill(self, attribution: OutcomeAttribution) -> DistilledLesson:
            del attribution
            return DistilledLesson(
                abstract_statement="Abstract claim",
                mechanism_code="abstract_claim",
                raw_story_present=True,
            )

    raw_loop, *_ = _loop(_RawStoryDistiller())
    with pytest.raises(LearningRejected, match="raw tenant story"):
        raw_loop.process(_attribution(tenant_id), _triage(LearningScope.GENERAL), now=_NOW)


def test_general_path_requires_name_registry_and_rejects_registered_name() -> None:
    tenant_id = uuid4()
    loop, *_ = _loop(_Distiller("Riya Sharma responded to the follow-up"))
    with pytest.raises(LearningRejected, match="requires tenant name-registry"):
        loop.process(_attribution(tenant_id), _triage(LearningScope.GENERAL), now=_NOW)
    with pytest.raises(LearningRejected, match="redaction token"):
        loop.process(
            _attribution(tenant_id),
            _triage(LearningScope.GENERAL),
            now=_NOW,
            name_registry=lambda candidate: candidate.casefold() == "riya sharma",
        )


def test_repeatable_loop_captures_below_k_but_serves_nothing() -> None:
    loop, _, contributions, _, priors = _loop(_Distiller())
    for _ in range(10):
        result = loop.process(
            _attribution(uuid4(), repeatable=True),
            _triage(LearningScope.REPEATABLE_AGGREGATE),
            now=_NOW,
        )
    assert len(contributions.for_key(_key())) == 10
    assert result.prior_decision is not None
    assert result.prior_decision.disposition is PriorDisposition.REJECTED
    assert priors.candidates == []


def test_public_models_have_no_tenant_identifier_field() -> None:
    from orchestrator.knowledge.learning_loop import GeneralLessonCandidate, PriorCandidate

    for model in (GeneralLessonCandidate, PriorCandidate):
        assert "tenant_id" not in model.model_fields
    serialized_schema = json.dumps(PriorCandidate.model_json_schema(), sort_keys=True)
    assert "raw_story" not in serialized_schema
    assert "lesson_input" not in serialized_schema
