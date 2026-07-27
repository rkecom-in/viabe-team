"""VT-711 corpus versioning, O11 admission, ablation and demotion tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.admission import (  # noqa: E402
    AblationPolicy,
    AblationVerdict,
    AdmissionPolicy,
    AdmissionRejected,
    AdmissionVerdict,
    CardAblationEvaluator,
    CardDemotionController,
    CorpusAdmissionController,
    DemotionAction,
    IncidentCategory,
    KnowledgeIncident,
    O11RunSummary,
    build_corpus_version,
)
from orchestrator.knowledge.contracts import (  # noqa: E402
    Applicability,
    CardProvenance,
    CardStatus,
    ClaimKey,
    ClaimValueType,
    CorpusVersion,
    CorpusVersionStatus,
    EvidenceAuthority,
    EvidenceConfidence,
    KnowledgeCard,
    KnowledgeDomain,
    KnowledgeScopeKind,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
)

_NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
_DIGEST = "a" * 64
_DIMS = {
    "decision_correctness": 0.6,
    "applicability": 0.6,
    "regulatory_financial_safety": 0.6,
}
_SLICES = {"money": 0.7, "consent": 0.7, "regulatory": 0.7}


def _run(
    *,
    corpus: str,
    mode: str,
    mean: float,
    bump: float = 0.0,
    scenario_scores: dict[str, float] | None = None,
    slices: dict[str, float] | None = None,
    digest: str = _DIGEST,
    hard_failures: int = 0,
) -> O11RunSummary:
    scores = scenario_scores or {f"opaque-{index}": mean for index in range(4)}
    return O11RunSummary(
        corpus_version_id=corpus,
        knowledge_mode=mode,
        dataset_partition="validation",
        dataset_digest=digest,
        run_ref=f"run:{corpus}:{mode}:{mean}",
        evaluator_id="o11:judge-v1",
        agent_version="git:same-agent",
        sample_size=len(scores),
        mean_score=mean,
        dimension_means={key: value + bump for key, value in _DIMS.items()},
        safety_slice_means=slices or {key: value + bump for key, value in _SLICES.items()},
        scenario_scores=scores,
        hard_failure_count=hard_failures,
    )


def _corpus(status: CorpusVersionStatus = CorpusVersionStatus.CANDIDATE) -> CorpusVersion:
    return CorpusVersion(
        corpus_version_id="corpus-candidate",
        parent_version_id="corpus-baseline",
        status=status,
        card_version_ids=("card-v1",),
        content_digest="b" * 64,
        created_at=_NOW,
        created_by="vt711-test",
    )


def _policy(**updates) -> AdmissionPolicy:
    values = {
        "minimum_sample_size": 4,
        "confidence_z": 1.96,
        "minimum_mean_improvement": 0.1,
        "minimum_dimension_delta": -0.02,
        "safety_noninferiority_margins": {
            "money": 0.01,
            "consent": 0.01,
            "regulatory": 0.01,
        },
        "maximum_treatment_hard_failures": 0,
    }
    values.update(updates)
    return AdmissionPolicy(**values)


def test_default_admission_is_pending_and_cannot_validate_corpus() -> None:
    decision = CorpusAdmissionController().evaluate(
        _corpus(),
        baseline=_run(corpus="corpus-baseline", mode="off", mean=0.6),
        treatment=_run(corpus="corpus-candidate", mode="shadow", mean=0.8, bump=0.2),
    )
    assert decision.verdict is AdmissionVerdict.PENDING_POLICY
    assert decision.admitted_version is None
    assert decision.evaluation_records == ()
    assert decision.reasons == ("graduation_thresholds_not_approved",)


def test_explicit_policy_admits_only_with_confident_improvement_and_safe_slices() -> None:
    baseline = _run(corpus="corpus-baseline", mode="off", mean=0.6)
    treatment = _run(corpus="corpus-candidate", mode="shadow", mean=0.8, bump=0.2)
    decision = CorpusAdmissionController(_policy()).evaluate(
        _corpus(), baseline=baseline, treatment=treatment, evaluated_at=_NOW
    )
    assert decision.verdict is AdmissionVerdict.ADMIT
    assert decision.admitted_version is not None
    assert decision.admitted_version.status is CorpusVersionStatus.VALIDATED
    assert len(decision.evaluation_records) == 2
    assert all(record.corpus_version_id in {"corpus-baseline", "corpus-candidate"} for record in decision.evaluation_records)


def test_safety_slice_regression_rejects_otherwise_better_corpus() -> None:
    baseline = _run(corpus="corpus-baseline", mode="off", mean=0.6)
    treatment = _run(
        corpus="corpus-candidate",
        mode="shadow",
        mean=0.8,
        bump=0.2,
        slices={"money": 0.2, "consent": 0.9, "regulatory": 0.9},
    )
    decision = CorpusAdmissionController(_policy()).evaluate(
        _corpus(), baseline=baseline, treatment=treatment
    )
    assert decision.verdict is AdmissionVerdict.REJECT
    assert "safety_noninferiority_failed:money" in decision.reasons
    assert decision.admitted_version is None


def test_ab_comparison_rejects_dataset_or_agent_drift() -> None:
    baseline = _run(corpus="corpus-baseline", mode="off", mean=0.6)
    treatment = _run(
        corpus="corpus-candidate", mode="shadow", mean=0.8, bump=0.2, digest="c" * 64
    )
    with pytest.raises(AdmissionRejected, match="dataset digests differ"):
        CorpusAdmissionController(_policy()).evaluate(
            _corpus(), baseline=baseline, treatment=treatment
        )


def test_sealed_public_report_rejects_case_details() -> None:
    report = {
        "dataset_split": "sealed",
        "dataset_digest": _DIGEST,
        "knowledge_mode": "off",
        "agent_version": "git:x",
        "judge": "o11",
        "case_count": 2,
        "mean_score": 0.5,
        "dimension_means": {"decision_correctness": 0.5},
        "hard_failure_count": 0,
        "cases": [{"case_id": "must-not-leak"}],
    }
    with pytest.raises(AdmissionRejected, match="must not expose case details"):
        O11RunSummary.from_o11_report(
            report,
            corpus_version_id="baseline",
            run_ref="sealed-run",
            safety_slice_means=_SLICES,
        )


def _card(version_id: str = "card-v1") -> KnowledgeCard:
    return KnowledgeCard(
        card_id="card-one",
        card_version_id=version_id,
        card_version=1,
        claim="Review customer response before repeating the tactic.",
        distillation_note="A candidate business lesson.",
        claim_key=ClaimKey(
            subject="follow_up",
            predicate="review_response",
            jurisdiction="india",
            population="micro_business",
            channel="whatsapp",
        ),
        claim_value=TypedClaimValue(value_type=ClaimValueType.TEXT, value="review"),
        source_class=SourceClass.T3_PRACTITIONER,
        domain=KnowledgeDomain.SALES,
        authority=EvidenceAuthority.SEED,
        confidence=EvidenceConfidence.MEDIUM,
        independence_cluster="cluster-one",
        applicability=Applicability(jurisdictions=("India",)),
        provenance=CardProvenance(
            source_ids=("source-one",), publisher="RKECOM", retrieved_at=_NOW, tainted=True
        ),
        usage_rights=UsageRights(
            status=UsageRightsStatus.PERMISSION_GRANTED,
            allows_extraction=True,
            allows_embedding=True,
            allows_retrieval=True,
            reviewed_at=_NOW,
            reviewed_by="owner",
        ),
        retention_class="lifecycle_managed",
        scope=KnowledgeScopeKind.GLOBAL,
        status=CardStatus.CANDIDATE,
        retrieval_eligible=False,
    )


def test_corpus_version_digest_is_deterministic_and_candidate_only() -> None:
    first = build_corpus_version(
        [_card("card-v2"), _card("card-v1")],
        corpus_version_id="candidate-two",
        parent_version_id="baseline",
        created_by="test",
        created_at=_NOW,
    )
    second = build_corpus_version(
        [_card("card-v1"), _card("card-v2")],
        corpus_version_id="candidate-two",
        parent_version_id="baseline",
        created_by="test",
        created_at=_NOW,
    )
    assert first.content_digest == second.content_digest
    assert first.card_version_ids == ("card-v1", "card-v2")
    assert first.status is CorpusVersionStatus.CANDIDATE


def test_ablation_must_reproduce_harm_across_scenarios_before_supersession() -> None:
    with_card = _run(
        corpus="corpus-candidate",
        mode="active",
        mean=0.5,
        scenario_scores={"opaque-a": 0.4, "opaque-b": 0.5, "opaque-c": 0.6},
    )
    without = _run(
        corpus="corpus-candidate",
        mode="ablation_without_card",
        mean=0.8,
        scenario_scores={"opaque-a": 0.8, "opaque-b": 0.8, "opaque-c": 0.8},
    )
    ablation = CardAblationEvaluator(
        AblationPolicy(minimum_reproduced_scenarios=2, minimum_harmful_delta=0.2)
    ).evaluate(
        card_version_ref="card-v1",
        with_card=with_card,
        without_card=without,
        evaluated_at=_NOW,
    )
    assert ablation.verdict is AblationVerdict.CAUSAL_REGRESSION_REPRODUCED
    decision = CardDemotionController().permanent_supersede(
        card_version_ref="card-v1",
        current_status=CardStatus.EMERGENCY_QUARANTINED,
        ablation=ablation,
        actor_id="governance:vt711",
        occurred_at=_NOW,
    )
    assert decision.action is DemotionAction.PERMANENT_SUPERSEDE
    assert decision.transition is not None
    assert decision.transition.to_status is CardStatus.SUPERSEDED


def test_non_reproduced_ablation_cannot_permanently_demote() -> None:
    with_card = _run(
        corpus="same",
        mode="active",
        mean=0.7,
        scenario_scores={"opaque-a": 0.7, "opaque-b": 0.7},
    )
    without = _run(
        corpus="same",
        mode="without",
        mean=0.71,
        scenario_scores={"opaque-a": 0.71, "opaque-b": 0.71},
    )
    ablation = CardAblationEvaluator(
        AblationPolicy(minimum_reproduced_scenarios=2, minimum_harmful_delta=0.2)
    ).evaluate(card_version_ref="card-v1", with_card=with_card, without_card=without)
    decision = CardDemotionController().permanent_supersede(
        card_version_ref="card-v1",
        current_status=CardStatus.VALIDATED,
        ablation=ablation,
        actor_id="governance:vt711",
    )
    assert ablation.verdict is AblationVerdict.NOT_REPRODUCED
    assert decision.action is DemotionAction.NO_ACTION
    assert decision.transition is None


def test_critical_incident_quarantines_immediately_but_quality_incident_does_not() -> None:
    controller = CardDemotionController()
    critical = KnowledgeIncident(
        incident_id=uuid4(),
        tenant_id=uuid4(),
        card_version_ref="card-v1",
        category=IncidentCategory.CONSENT,
        detected_at=_NOW,
        evidence_refs=(uuid4(),),
    )
    decision = controller.emergency_quarantine(
        card_version_ref="card-v1",
        current_status=CardStatus.VALIDATED,
        incident=critical,
        actor_id="monitor:vt711",
    )
    assert decision.action is DemotionAction.EMERGENCY_QUARANTINE
    assert decision.transition is not None and decision.transition.emergency is True
    assert str(critical.tenant_id) not in critical.model_dump_json()
    assert str(critical.tenant_id) not in decision.transition.reason

    quality = critical.model_copy(
        update={"incident_id": uuid4(), "category": IncidentCategory.QUALITY}
    )
    no_action = controller.emergency_quarantine(
        card_version_ref="card-v1",
        current_status=CardStatus.VALIDATED,
        incident=quality,
        actor_id="monitor:vt711",
    )
    assert no_action.action is DemotionAction.NO_ACTION
