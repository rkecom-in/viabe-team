"""VT-711 O8 corpus admission, O11 A/B, ablation and demotion machinery.

This module evaluates supplied evidence; it does not run the sealed set, call an LLM, mutate a
registry, or activate a corpus.  The default admission controller has no thresholds and therefore
returns ``pending_policy``.  Fazal-approved sample sizes, confidence bounds and non-inferiority
margins must be injected before a corpus can receive an ``admit`` verdict.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from statistics import fmean, stdev
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.knowledge.contracts import (
    CardLifecycleTransition,
    CardStatus,
    CorpusVersion,
    CorpusVersionStatus,
    KnowledgeCard,
)

_EVALUATION_NAMESPACE = UUID("b444a0f3-854c-4a8c-8665-4feff8426a8f")
_SAFETY_SLICES = frozenset({"money", "consent", "regulatory"})


class AdmissionRejected(RuntimeError):
    """Evaluation inputs are not comparable or violate custody/governance rules."""


class AdmissionVerdict(StrEnum):
    PENDING_POLICY = "pending_policy"
    ADMIT = "admit"
    REJECT = "reject"


class EvaluationKind(StrEnum):
    BASELINE = "baseline"
    TREATMENT = "treatment"
    ABLATION = "ablation"
    SAFETY_SLICE = "safety_slice"


class AblationVerdict(StrEnum):
    CAUSAL_REGRESSION_REPRODUCED = "causal_regression_reproduced"
    NOT_REPRODUCED = "not_reproduced"


class DemotionAction(StrEnum):
    EMERGENCY_QUARANTINE = "emergency_quarantine"
    PERMANENT_SUPERSEDE = "permanent_supersede"
    NO_ACTION = "no_action"


class O11RunSummary(BaseModel):
    """Content-free O11 run evidence bound to one corpus version."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    corpus_version_id: str = Field(min_length=1, max_length=200)
    knowledge_mode: str = Field(min_length=1, max_length=80)
    dataset_partition: Literal["development", "validation", "sealed"]
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_ref: str = Field(min_length=1, max_length=300)
    evaluator_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    sample_size: int = Field(ge=1)
    mean_score: float = Field(ge=0.0, le=1.0)
    dimension_means: dict[str, float]
    safety_slice_means: dict[str, float]
    scenario_scores: dict[str, float] = Field(default_factory=dict, exclude=True)
    hard_failure_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _scores_are_bounded_and_complete(self) -> "O11RunSummary":
        if not self.dimension_means:
            raise ValueError("dimension_means cannot be empty")
        for label, values in (
            ("dimension_means", self.dimension_means),
            ("safety_slice_means", self.safety_slice_means),
            ("scenario_scores", self.scenario_scores),
        ):
            if any(not 0.0 <= score <= 1.0 for score in values.values()):
                raise ValueError(f"{label} scores must be within 0..1")
        if self.scenario_scores and len(self.scenario_scores) != self.sample_size:
            raise ValueError("scenario_scores coverage must equal sample_size")
        return self

    @classmethod
    def from_o11_report(
        cls,
        report: Mapping[str, Any],
        *,
        corpus_version_id: str,
        run_ref: str,
        safety_slice_means: Mapping[str, float],
        scenario_scores: Mapping[str, float] | None = None,
    ) -> "O11RunSummary":
        """Bind the existing O11 public report shape to a corpus evaluation record.

        Sealed reports must remain aggregate-only: case details are rejected.  A sealed custodian
        may supply opaque per-scenario scores directly to a later ablation process without exposing
        scenario content; this builder is never an excuse to place the sealed dataset in-repo.
        """

        partition = str(report.get("dataset_split", ""))
        if partition not in {"development", "validation", "sealed"}:
            raise AdmissionRejected("O11 report has an invalid dataset partition")
        if partition == "sealed" and "cases" in report:
            raise AdmissionRejected("sealed O11 report must not expose case details")
        dimensions = report.get("dimension_means")
        if not isinstance(dimensions, Mapping):
            raise AdmissionRejected("O11 report lacks dimension_means")
        return cls(
            corpus_version_id=corpus_version_id,
            knowledge_mode=str(report.get("knowledge_mode", "")),
            dataset_partition=cast(
                Literal["development", "validation", "sealed"], partition
            ),
            dataset_digest=str(report.get("dataset_digest", "")),
            run_ref=run_ref,
            evaluator_id=str(report.get("judge", "")),
            agent_version=str(report.get("agent_version", "")),
            sample_size=int(report.get("case_count", 0)),
            mean_score=float(report.get("mean_score", -1.0)),
            dimension_means={str(key): float(value) for key, value in dimensions.items()},
            safety_slice_means={
                str(key): float(value) for key, value in safety_slice_means.items()
            },
            scenario_scores={
                str(key): float(value) for key, value in (scenario_scores or {}).items()
            },
            hard_failure_count=int(report.get("hard_failure_count", -1)),
        )


class AdmissionPolicy(BaseModel):
    """Fazal-approved graduation numbers; every field is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_sample_size: int = Field(ge=2)
    confidence_z: float = Field(gt=0.0, le=5.0)
    minimum_mean_improvement: float = Field(ge=0.0, le=1.0)
    minimum_dimension_delta: float = Field(ge=-1.0, le=1.0)
    safety_noninferiority_margins: dict[str, float]
    maximum_treatment_hard_failures: int = Field(ge=0)

    @model_validator(mode="after")
    def _all_safety_slices_declared(self) -> "AdmissionPolicy":
        if set(self.safety_noninferiority_margins) != _SAFETY_SLICES:
            raise ValueError(
                "safety_noninferiority_margins must declare money, consent and regulatory"
            )
        if any(not 0.0 <= margin <= 1.0 for margin in self.safety_noninferiority_margins.values()):
            raise ValueError("safety non-inferiority margins must be within 0..1")
        return self


class ABComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_digest: str
    sample_size: int
    mean_delta: float
    lower_confidence_bound: float | None
    dimension_deltas: dict[str, float]
    safety_slice_deltas: dict[str, float]
    hard_failure_delta: int


class EvaluationRecord(BaseModel):
    """Insert-ready shape for migration 182 ``knowledge_evaluations``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: UUID
    corpus_version_id: str
    baseline_corpus_version_id: str | None
    card_version_ref: str | None = None
    evaluation_kind: EvaluationKind
    dataset_partition: Literal["development", "validation", "sealed"]
    run_ref: str
    evaluator_id: str
    sample_size: int = Field(ge=0)
    metrics: dict[str, Any]
    passed: bool | None
    created_at: datetime

    @model_validator(mode="after")
    def _created_at_is_aware(self) -> "EvaluationRecord":
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


class CorpusAdmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AdmissionVerdict
    reasons: tuple[str, ...]
    comparison: ABComparison | None = None
    evaluation_records: tuple[EvaluationRecord, ...] = ()
    admitted_version: CorpusVersion | None = None

    @model_validator(mode="after")
    def _admitted_version_only_on_pass(self) -> "CorpusAdmissionDecision":
        if self.verdict is AdmissionVerdict.ADMIT and self.admitted_version is None:
            raise ValueError("admit verdict requires a validated corpus version")
        if self.verdict is not AdmissionVerdict.ADMIT and self.admitted_version is not None:
            raise ValueError("non-admit verdict cannot return a validated corpus")
        return self


def build_corpus_version(
    cards: Sequence[KnowledgeCard],
    *,
    corpus_version_id: str,
    parent_version_id: str | None,
    created_by: str,
    created_at: datetime | None = None,
) -> CorpusVersion:
    """Create a deterministic candidate snapshot; never validates its contents."""

    if not cards:
        raise AdmissionRejected("corpus version cannot be empty")
    ids = tuple(sorted(card.card_version_id for card in cards))
    if len(set(ids)) != len(ids):
        raise AdmissionRejected("corpus cannot contain duplicate card versions")
    canonical = [
        {
            "card_version_id": card.card_version_id,
            "claim_key": card.claim_key.canonical,
            "claim": card.claim,
            "status": card.status.value,
        }
        for card in sorted(cards, key=lambda item: item.card_version_id)
    ]
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CorpusVersion(
        corpus_version_id=corpus_version_id,
        parent_version_id=parent_version_id,
        status=CorpusVersionStatus.CANDIDATE,
        card_version_ids=ids,
        content_digest=digest,
        created_at=created_at or datetime.now(UTC),
        created_by=created_by,
    )


class O11ABHarness:
    """Pure comparison hook for baseline/treatment reports produced by the O11 harness."""

    @staticmethod
    def compare(
        baseline: O11RunSummary,
        treatment: O11RunSummary,
        *,
        confidence_z: float,
    ) -> ABComparison:
        _assert_comparable_runs(baseline, treatment)
        paired_deltas: list[float] = []
        lower_bound: float | None = None
        if baseline.scenario_scores or treatment.scenario_scores:
            if set(baseline.scenario_scores) != set(treatment.scenario_scores):
                raise AdmissionRejected("baseline/treatment scenario coverage differs")
            paired_deltas = [
                treatment.scenario_scores[key] - baseline.scenario_scores[key]
                for key in sorted(baseline.scenario_scores)
            ]
            if len(paired_deltas) >= 2:
                standard_error = stdev(paired_deltas) / math.sqrt(len(paired_deltas))
                lower_bound = fmean(paired_deltas) - confidence_z * standard_error
        dimensions = {
            key: treatment.dimension_means[key] - baseline.dimension_means[key]
            for key in baseline.dimension_means
        }
        slices = {
            key: treatment.safety_slice_means[key] - baseline.safety_slice_means[key]
            for key in baseline.safety_slice_means
        }
        return ABComparison(
            dataset_digest=baseline.dataset_digest,
            sample_size=baseline.sample_size,
            mean_delta=treatment.mean_score - baseline.mean_score,
            lower_confidence_bound=lower_bound,
            dimension_deltas=dimensions,
            safety_slice_deltas=slices,
            hard_failure_delta=treatment.hard_failure_count - baseline.hard_failure_count,
        )


class CorpusAdmissionController:
    """Default-unconfigured admission gate.  No policy means no graduation."""

    def __init__(self, policy: AdmissionPolicy | None = None):
        self._policy = policy

    def evaluate(
        self,
        corpus: CorpusVersion,
        *,
        baseline: O11RunSummary,
        treatment: O11RunSummary,
        evaluated_at: datetime | None = None,
    ) -> CorpusAdmissionDecision:
        if corpus.status not in {CorpusVersionStatus.CANDIDATE, CorpusVersionStatus.SHADOW}:
            raise AdmissionRejected("only candidate/shadow corpora can enter admission")
        if treatment.corpus_version_id != corpus.corpus_version_id:
            raise AdmissionRejected("treatment run is not bound to the candidate corpus")
        if self._policy is None:
            return CorpusAdmissionDecision(
                verdict=AdmissionVerdict.PENDING_POLICY,
                reasons=("graduation_thresholds_not_approved",),
            )

        comparison = O11ABHarness.compare(
            baseline, treatment, confidence_z=self._policy.confidence_z
        )
        reasons: list[str] = []
        if comparison.sample_size < self._policy.minimum_sample_size:
            reasons.append("minimum_sample_size_not_met")
        if comparison.lower_confidence_bound is None:
            reasons.append("paired_confidence_bound_unavailable")
        elif comparison.lower_confidence_bound < self._policy.minimum_mean_improvement:
            reasons.append("mean_improvement_confidence_bound_not_met")
        for dimension, delta in comparison.dimension_deltas.items():
            if delta < self._policy.minimum_dimension_delta:
                reasons.append(f"dimension_regression:{dimension}")
        if set(comparison.safety_slice_deltas) != _SAFETY_SLICES:
            reasons.append("safety_slice_coverage_incomplete")
        else:
            for name, margin in self._policy.safety_noninferiority_margins.items():
                if comparison.safety_slice_deltas[name] < -margin:
                    reasons.append(f"safety_noninferiority_failed:{name}")
        if treatment.hard_failure_count > self._policy.maximum_treatment_hard_failures:
            reasons.append("treatment_hard_failure_ceiling_exceeded")

        passed = not reasons
        created_at = evaluated_at or datetime.now(UTC)
        records = (
            _evaluation_record(
                run=baseline,
                kind=EvaluationKind.BASELINE,
                baseline_id=None,
                passed=None,
                metrics={"mean_score": baseline.mean_score},
                created_at=created_at,
            ),
            _evaluation_record(
                run=treatment,
                kind=EvaluationKind.TREATMENT,
                baseline_id=baseline.corpus_version_id,
                passed=passed,
                metrics=comparison.model_dump(mode="json"),
                created_at=created_at,
            ),
        )
        admitted = (
            corpus.model_copy(update={"status": CorpusVersionStatus.VALIDATED})
            if passed
            else None
        )
        return CorpusAdmissionDecision(
            verdict=AdmissionVerdict.ADMIT if passed else AdmissionVerdict.REJECT,
            reasons=("all_gates_passed",) if passed else tuple(reasons),
            comparison=comparison,
            evaluation_records=records,
            admitted_version=admitted,
        )


def _assert_comparable_runs(baseline: O11RunSummary, treatment: O11RunSummary) -> None:
    if baseline.dataset_digest != treatment.dataset_digest:
        raise AdmissionRejected("baseline/treatment dataset digests differ")
    if baseline.dataset_partition != treatment.dataset_partition:
        raise AdmissionRejected("baseline/treatment partitions differ")
    if baseline.sample_size != treatment.sample_size:
        raise AdmissionRejected("baseline/treatment sample sizes differ")
    if baseline.evaluator_id != treatment.evaluator_id:
        raise AdmissionRejected("baseline/treatment evaluators differ")
    if baseline.agent_version != treatment.agent_version:
        raise AdmissionRejected("A/B must isolate knowledge; agent versions differ")
    if set(baseline.dimension_means) != set(treatment.dimension_means):
        raise AdmissionRejected("baseline/treatment dimensions differ")
    if set(baseline.safety_slice_means) != set(treatment.safety_slice_means):
        raise AdmissionRejected("baseline/treatment safety slices differ")


def _evaluation_record(
    *,
    run: O11RunSummary,
    kind: EvaluationKind,
    baseline_id: str | None,
    passed: bool | None,
    metrics: dict[str, Any],
    created_at: datetime,
    card_version_ref: str | None = None,
) -> EvaluationRecord:
    identity = f"{run.corpus_version_id}|{kind.value}|{run.run_ref}|{card_version_ref or '-'}"
    return EvaluationRecord(
        evaluation_id=uuid5(_EVALUATION_NAMESPACE, identity),
        corpus_version_id=run.corpus_version_id,
        baseline_corpus_version_id=baseline_id,
        card_version_ref=card_version_ref,
        evaluation_kind=kind,
        dataset_partition=run.dataset_partition,
        run_ref=run.run_ref,
        evaluator_id=run.evaluator_id,
        sample_size=run.sample_size,
        metrics=metrics,
        passed=passed,
        created_at=created_at,
    )


class AblationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_reproduced_scenarios: int = Field(ge=2)
    minimum_harmful_delta: float = Field(gt=0.0, le=1.0)


class AblationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AblationVerdict
    card_version_ref: str
    reproduced_scenario_count: int
    scenario_count: int
    mean_without_minus_with: float
    evaluation_record: EvaluationRecord


class CardAblationEvaluator:
    """Counterfactual replay: same scenarios/agent/judge, with and without one card."""

    def __init__(self, policy: AblationPolicy):
        self._policy = policy

    def evaluate(
        self,
        *,
        card_version_ref: str,
        with_card: O11RunSummary,
        without_card: O11RunSummary,
        evaluated_at: datetime | None = None,
    ) -> AblationResult:
        _assert_comparable_runs(with_card, without_card)
        if not with_card.scenario_scores or not without_card.scenario_scores:
            raise AdmissionRejected("ablation requires opaque per-scenario scores")
        if set(with_card.scenario_scores) != set(without_card.scenario_scores):
            raise AdmissionRejected("ablation scenario coverage differs")
        deltas = {
            scenario: without_card.scenario_scores[scenario] - with_score
            for scenario, with_score in with_card.scenario_scores.items()
        }
        reproduced = sum(
            delta >= self._policy.minimum_harmful_delta for delta in deltas.values()
        )
        verdict = (
            AblationVerdict.CAUSAL_REGRESSION_REPRODUCED
            if reproduced >= self._policy.minimum_reproduced_scenarios
            else AblationVerdict.NOT_REPRODUCED
        )
        mean_delta = fmean(deltas.values())
        record = _evaluation_record(
            run=with_card,
            kind=EvaluationKind.ABLATION,
            baseline_id=without_card.corpus_version_id,
            passed=verdict is AblationVerdict.CAUSAL_REGRESSION_REPRODUCED,
            metrics={
                "reproduced_scenario_count": reproduced,
                "scenario_count": len(deltas),
                "mean_without_minus_with": mean_delta,
                "minimum_harmful_delta": self._policy.minimum_harmful_delta,
            },
            created_at=evaluated_at or datetime.now(UTC),
            card_version_ref=card_version_ref,
        )
        return AblationResult(
            verdict=verdict,
            card_version_ref=card_version_ref,
            reproduced_scenario_count=reproduced,
            scenario_count=len(deltas),
            mean_without_minus_with=mean_delta,
            evaluation_record=record,
        )


class IncidentCategory(StrEnum):
    MONEY = "money"
    REGULATORY = "regulatory"
    CONSENT = "consent"
    CROSS_TENANT_EVIDENCE = "cross_tenant_evidence"
    PROVENANCE_LOSS = "provenance_loss"
    QUALITY = "quality"


class KnowledgeIncident(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: UUID
    tenant_id: UUID = Field(exclude=True)
    card_version_ref: str
    category: IncidentCategory
    detected_at: datetime
    evidence_refs: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _aware_time(self) -> "KnowledgeIncident":
        if self.detected_at.utcoffset() is None:
            raise ValueError("detected_at must be timezone-aware")
        return self


class DemotionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: DemotionAction
    reason: str
    transition: CardLifecycleTransition | None = None


class CardDemotionController:
    """Emergency quarantine is immediate; permanent supersession needs ablation causality."""

    def emergency_quarantine(
        self,
        *,
        card_version_ref: str,
        current_status: CardStatus,
        incident: KnowledgeIncident,
        actor_id: str,
    ) -> DemotionDecision:
        if incident.card_version_ref != card_version_ref:
            raise AdmissionRejected("incident is not attributed to the card")
        if incident.category not in {
            IncidentCategory.MONEY,
            IncidentCategory.REGULATORY,
            IncidentCategory.CONSENT,
            IncidentCategory.CROSS_TENANT_EVIDENCE,
            IncidentCategory.PROVENANCE_LOSS,
        }:
            return DemotionDecision(
                action=DemotionAction.NO_ACTION,
                reason="non-critical incident requires evaluation before lifecycle mutation",
            )
        transition = CardLifecycleTransition(
            transition_id=uuid5(_EVALUATION_NAMESPACE, f"quarantine|{incident.incident_id}"),
            card_version_id=card_version_ref,
            from_status=current_status,
            to_status=CardStatus.EMERGENCY_QUARANTINED,
            reason=f"incident:{incident.category.value}:{incident.incident_id}",
            actor_id=actor_id,
            idempotency_key=f"o8-quarantine:{incident.incident_id}",
            occurred_at=incident.detected_at,
            emergency=True,
        )
        return DemotionDecision(
            action=DemotionAction.EMERGENCY_QUARANTINE,
            reason="critical incident triggers reversible immediate quarantine",
            transition=transition,
        )

    def permanent_supersede(
        self,
        *,
        card_version_ref: str,
        current_status: CardStatus,
        ablation: AblationResult,
        actor_id: str,
        occurred_at: datetime | None = None,
    ) -> DemotionDecision:
        if (
            ablation.card_version_ref != card_version_ref
            or ablation.verdict is not AblationVerdict.CAUSAL_REGRESSION_REPRODUCED
        ):
            return DemotionDecision(
                action=DemotionAction.NO_ACTION,
                reason="permanent demotion requires reproduced card ablation",
            )
        when = occurred_at or datetime.now(UTC)
        transition = CardLifecycleTransition(
            transition_id=uuid5(
                _EVALUATION_NAMESPACE,
                f"supersede|{card_version_ref}|{ablation.evaluation_record.evaluation_id}",
            ),
            card_version_id=card_version_ref,
            from_status=current_status,
            to_status=CardStatus.SUPERSEDED,
            reason=f"ablation:{ablation.evaluation_record.evaluation_id}",
            actor_id=actor_id,
            idempotency_key=(
                f"o8-supersede:{card_version_ref}:{ablation.evaluation_record.evaluation_id}"
            ),
            occurred_at=when,
        )
        return DemotionDecision(
            action=DemotionAction.PERMANENT_SUPERSEDE,
            reason="counterfactual replay reproduced the harmful card effect",
            transition=transition,
        )


__all__ = [
    "ABComparison",
    "AblationPolicy",
    "AblationResult",
    "AblationVerdict",
    "AdmissionPolicy",
    "AdmissionRejected",
    "AdmissionVerdict",
    "CardAblationEvaluator",
    "CardDemotionController",
    "CorpusAdmissionController",
    "CorpusAdmissionDecision",
    "DemotionAction",
    "DemotionDecision",
    "EvaluationKind",
    "EvaluationRecord",
    "IncidentCategory",
    "KnowledgeIncident",
    "O11ABHarness",
    "O11RunSummary",
    "build_corpus_version",
]
