"""VT-711 O8 learning-loop and privacy-preserving L3-prior admission machinery.

The module is storage-agnostic and deliberately has no live caller.  Tenant lessons are written
only through an L1/L2 tenant sink.  Cross-tenant learning is built from bounded, structured
0..1 observations; raw stories and tenant identifiers have no field on an L3 prior candidate.
General lessons enter a *candidate* sink, never the trusted registry.

The L3 gate reuses VT-225/VT-68's ``check_contributor_admission`` at execution time.  The adapter
returns only the decision/count; its eligible tenant identifiers remain inside the admission
function and are never copied onto a prior or audit record.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from statistics import fmean
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.knowledge_contracts import KnowledgeLayer

K_ANON_FLOOR = 10
_NORMALIZED_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_UUIDISH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_REDACTION_TOKEN_RE = re.compile(
    r"phone_tok_|body_tok_|<(?:customer_name|owner_name)>|"
    r"<(?:email|phone|pan|aadhaar|ifsc|gst|cc|bank|redacted):",
    re.IGNORECASE,
)


class LearningRejected(RuntimeError):
    """A privacy/governance invariant rejected a learning-loop write."""


class LearningScope(StrEnum):
    TENANT = "tenant"
    REPEATABLE_AGGREGATE = "repeatable_aggregate"
    GENERAL = "general"


class TriageAuthority(StrEnum):
    DETERMINISTIC_POLICY = "deterministic_policy"
    HUMAN_REVIEWER = "human_reviewer"
    EVALUATION_HARNESS = "evaluation_harness"


class PriorDisposition(StrEnum):
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    CANDIDATE = "candidate"


class PriorKey(BaseModel):
    """Coarsened identity for a repeatable lesson; structurally excludes locality/customer data."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    subject: str
    predicate: str
    jurisdiction: str
    business_archetype: str
    size_band: str
    maturity_stage: str
    channel: str

    @field_validator("*")
    @classmethod
    def _coarse_normalized_dimension(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        if not _NORMALIZED_RE.fullmatch(normalized):
            raise ValueError("prior dimensions must be coarse normalized labels")
        if _UUIDISH_RE.search(normalized) or "@" in normalized:
            raise ValueError("prior dimensions cannot contain identifiers")
        return normalized

    @property
    def canonical(self) -> str:
        return "|".join(
            (
                self.subject,
                self.predicate,
                self.jurisdiction,
                self.business_archetype,
                self.size_band,
                self.maturity_stage,
                self.channel,
            )
        )


class OutcomeAttribution(BaseModel):
    """Tenant-scoped evidence entering distillation; never itself a global payload."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    tenant_id: UUID = Field(exclude=True)
    outcome_ref: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    attributed_card_version_ids: tuple[str, ...] = ()
    lesson_input: str = Field(min_length=1, max_length=4_000, exclude=True)
    prior_key: PriorKey | None = None
    normalized_outcome: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _aware_and_repeatable_complete(self) -> "OutcomeAttribution":
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if (self.prior_key is None) != (self.normalized_outcome is None):
            raise ValueError("prior_key and normalized_outcome must be supplied together")
        return self


class DistilledLesson(BaseModel):
    """Tool-less lesson output.  Scope and admission authority are intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    abstract_statement: str = Field(min_length=1, max_length=2_000)
    mechanism_code: str
    raw_story_present: bool = False

    @field_validator("mechanism_code")
    @classmethod
    def _normalized_mechanism(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        if not _NORMALIZED_RE.fullmatch(normalized):
            raise ValueError("mechanism_code must be a normalized label")
        return normalized


class ToollessLessonDistiller(Protocol):
    tools_enabled: Literal[False]

    def distill(self, attribution: OutcomeAttribution) -> DistilledLesson: ...


class ScopeTriageDecision(BaseModel):
    """Governed scope decision.  Agent/model is deliberately not a valid final authority."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    scope: LearningScope
    authority: TriageAuthority
    decision_ref: str = Field(min_length=1, max_length=200)
    decided_at: datetime
    tenant_layer: KnowledgeLayer | None = None

    @model_validator(mode="after")
    def _layer_matches_scope(self) -> "ScopeTriageDecision":
        if self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        if self.scope is LearningScope.TENANT:
            if self.tenant_layer not in {KnowledgeLayer.L1, KnowledgeLayer.L2}:
                raise ValueError("tenant lessons must target L1 or L2")
        elif self.tenant_layer is not None:
            raise ValueError("global candidate paths cannot declare a tenant layer")
        return self


class TenantLessonRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID = Field(exclude=True)
    layer: KnowledgeLayer
    outcome_ref: str
    statement: str
    mechanism_code: str
    evidence_refs: tuple[str, ...]
    created_at: datetime

    @model_validator(mode="after")
    def _tenant_only(self) -> "TenantLessonRecord":
        if self.layer not in {KnowledgeLayer.L1, KnowledgeLayer.L2}:
            raise ValueError("tenant lesson layer must be L1/L2")
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


class GeneralLessonCandidate(BaseModel):
    """Governed global *candidate*, never a validated/trusted write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    statement: str
    mechanism_code: str
    evidence_refs: tuple[str, ...]
    status: Literal["candidate"] = "candidate"
    retrieval_eligible: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def _created_at_is_aware(self) -> "GeneralLessonCandidate":
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


class PriorContribution(BaseModel):
    """Tenant-scoped contribution retained behind an RLS-capable store adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contribution_id: str = Field(min_length=1, max_length=200)
    tenant_id: UUID = Field(exclude=True)
    key: PriorKey
    normalized_outcome: float = Field(ge=0.0, le=1.0)
    occurred_at: datetime
    captured_at: datetime
    evidence_ref_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _timestamps_are_aware(self) -> "PriorContribution":
        for name in ("occurred_at", "captured_at"):
            if getattr(self, name).utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self


class PriorAdmissionPolicy(BaseModel):
    """All unratified privacy/quality thresholds are required inputs, never hidden defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k_min: int = Field(ge=K_ANON_FLOOR)
    differencing_buffer_tenants: int = Field(ge=1)
    max_contributions_per_tenant: int = Field(ge=1)
    max_tenant_share: float = Field(gt=0.0, le=0.1)
    minimum_window_tenants: int = Field(ge=2)
    max_window_mean_delta: float = Field(ge=0.0, le=1.0)
    max_leave_one_tenant_out_delta: float = Field(ge=0.0, le=1.0)
    quarantine_days: int = Field(ge=1)
    published_decimal_places: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def _tenant_share_tracks_k_floor(self) -> "PriorAdmissionPolicy":
        if self.max_tenant_share > 1 / self.k_min:
            raise ValueError("max_tenant_share cannot exceed one contributor at the k floor")
        return self


class KAnonDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    tenant_count: int = Field(ge=0)
    reason: str


KAnonGate = Callable[[set[UUID], str, int], KAnonDecision]


def vt225_k_anon_gate(tenant_ids: set[UUID], cohort_key: str, k_min: int) -> KAnonDecision:
    """Adapter to the existing VT-225/VT-68 contributor gate; strips contributor UUIDs."""

    from orchestrator.privacy.k_anonymity import check_contributor_admission

    result = check_contributor_admission(tenant_ids, cohort_key, k_min=k_min)
    return KAnonDecision(
        admitted=result.admitted,
        tenant_count=result.tenant_count,
        reason=result.reason,
    )


class PriorCandidate(BaseModel):
    """An anonymized L3 candidate.  There is no tenant/story/source-event field by design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    key: PriorKey
    aggregate_mean: float = Field(ge=0.0, le=1.0)
    contributor_band: str
    contribution_count_band: str
    stability_delta: float = Field(ge=0.0, le=1.0)
    max_leave_one_out_delta: float = Field(ge=0.0, le=1.0)
    quarantine_until: datetime
    status: Literal["candidate"] = "candidate"
    retrieval_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _quarantine_time_is_aware(self) -> "PriorCandidate":
        if self.quarantine_until.utcoffset() is None:
            raise ValueError("quarantine_until must be timezone-aware")
        return self


class PriorAdmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: PriorDisposition
    reasons: tuple[str, ...]
    capped_contribution_count: int = Field(ge=0)
    distinct_tenant_count: int = Field(ge=0)
    candidate: PriorCandidate | None = None

    @model_validator(mode="after")
    def _candidate_matches_disposition(self) -> "PriorAdmissionDecision":
        if self.disposition is PriorDisposition.REJECTED and self.candidate is not None:
            raise ValueError("rejected prior cannot include a candidate")
        if self.disposition is not PriorDisposition.REJECTED and self.candidate is None:
            raise ValueError("accepted/quarantined prior requires a candidate")
        return self


class PriorBuilder:
    """Cap, k-gate, stability-check and differencing-check repeatable observations."""

    def __init__(self, policy: PriorAdmissionPolicy, *, k_gate: KAnonGate = vt225_k_anon_gate):
        self._policy = policy
        self._k_gate = k_gate

    def assess(
        self,
        contributions: Sequence[PriorContribution],
        *,
        now: datetime | None = None,
    ) -> PriorAdmissionDecision:
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not contributions:
            return PriorAdmissionDecision(
                disposition=PriorDisposition.REJECTED,
                reasons=("no_contributions",),
                capped_contribution_count=0,
                distinct_tenant_count=0,
            )

        keys = {item.key.canonical for item in contributions}
        if len(keys) != 1:
            raise LearningRejected("one prior assessment cannot mix prior keys")
        unique: dict[str, PriorContribution] = {}
        for item in contributions:
            existing = unique.get(item.contribution_id)
            if existing is not None and existing != item:
                raise LearningRejected("contribution_id collision with different content")
            unique[item.contribution_id] = item

        per_tenant: dict[UUID, list[PriorContribution]] = defaultdict(list)
        for item in unique.values():
            per_tenant[item.tenant_id].append(item)
        capped: list[PriorContribution] = []
        for items in per_tenant.values():
            items.sort(key=lambda item: (item.occurred_at, item.contribution_id))
            capped.extend(items[: self._policy.max_contributions_per_tenant])
        capped.sort(key=lambda item: (item.occurred_at, item.contribution_id))

        tenants = {item.tenant_id for item in capped}
        k_decision = self._k_gate(tenants, contributions[0].key.canonical, self._policy.k_min)
        if k_decision.tenant_count != len(tenants):
            raise LearningRejected("k-anonymity gate count disagrees with capped contributor set")
        reasons: list[str] = []
        if not k_decision.admitted:
            reasons.append(f"k_gate:{k_decision.reason}")
        if len(tenants) < self._policy.k_min + self._policy.differencing_buffer_tenants:
            reasons.append("differencing_buffer_not_met")

        total = len(capped)
        largest_share = max((len(items[: self._policy.max_contributions_per_tenant]) / total) for items in per_tenant.values())
        if largest_share > self._policy.max_tenant_share:
            reasons.append("tenant_contribution_share_exceeded")

        midpoint = max(1, len(capped) // 2)
        early, late = capped[:midpoint], capped[midpoint:]
        early_tenants = {item.tenant_id for item in early}
        late_tenants = {item.tenant_id for item in late}
        if (
            len(early_tenants) < self._policy.minimum_window_tenants
            or len(late_tenants) < self._policy.minimum_window_tenants
        ):
            reasons.append("stability_windows_too_small")
            stability_delta = 1.0
        else:
            stability_delta = abs(
                fmean(item.normalized_outcome for item in early)
                - fmean(item.normalized_outcome for item in late)
            )
            if stability_delta > self._policy.max_window_mean_delta:
                reasons.append("stability_delta_exceeded")

        overall = fmean(item.normalized_outcome for item in capped)
        leave_one_out_deltas: list[float] = []
        for tenant_id in tenants:
            remainder = [item.normalized_outcome for item in capped if item.tenant_id != tenant_id]
            if not remainder:
                leave_one_out_deltas.append(1.0)
            else:
                leave_one_out_deltas.append(abs(overall - fmean(remainder)))
        max_leave_one_out = max(leave_one_out_deltas, default=1.0)
        if max_leave_one_out > self._policy.max_leave_one_tenant_out_delta:
            reasons.append("leave_one_tenant_out_delta_exceeded")

        if reasons:
            return PriorAdmissionDecision(
                disposition=PriorDisposition.REJECTED,
                reasons=tuple(reasons),
                capped_contribution_count=len(capped),
                distinct_tenant_count=len(tenants),
            )

        latest = max(item.captured_at for item in capped)
        quarantine_until = latest + timedelta(days=self._policy.quarantine_days)
        digest_payload = (
            f"{contributions[0].key.canonical}|{round(overall, self._policy.published_decimal_places)}|"
            f"{_count_band(len(tenants))}|{_count_band(len(capped))}|{quarantine_until.isoformat()}"
        )
        candidate = PriorCandidate(
            candidate_id=hashlib.sha256(digest_payload.encode()).hexdigest(),
            key=contributions[0].key,
            aggregate_mean=round(overall, self._policy.published_decimal_places),
            contributor_band=_count_band(len(tenants)),
            contribution_count_band=_count_band(len(capped)),
            stability_delta=round(stability_delta, self._policy.published_decimal_places),
            max_leave_one_out_delta=round(
                max_leave_one_out, self._policy.published_decimal_places
            ),
            quarantine_until=quarantine_until,
        )
        disposition = (
            PriorDisposition.CANDIDATE
            if current >= quarantine_until
            else PriorDisposition.QUARANTINED
        )
        return PriorAdmissionDecision(
            disposition=disposition,
            reasons=("admitted",),
            capped_contribution_count=len(capped),
            distinct_tenant_count=len(tenants),
            candidate=candidate,
        )


def _count_band(value: int) -> str:
    """Publish coarse bands so adjacent corpus snapshots do not expose exact cohort deltas."""

    for ceiling in (10, 20, 50, 100, 250, 500, 1_000):
        if value <= ceiling:
            return f"up_to_{ceiling}"
    return "over_1000"


class TenantMemorySink(Protocol):
    def add(self, record: TenantLessonRecord) -> None: ...


class PriorContributionStore(Protocol):
    def add(self, contribution: PriorContribution) -> None: ...

    def for_key(self, key: PriorKey) -> Sequence[PriorContribution]: ...


class GeneralCandidateSink(Protocol):
    def add(self, candidate: GeneralLessonCandidate) -> None: ...


class PriorCandidateSink(Protocol):
    def add(self, candidate: PriorCandidate) -> None: ...


class LearningResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: LearningScope
    destination: str
    prior_decision: PriorAdmissionDecision | None = None


class LearningLoop:
    """Outcome → tool-less lesson → governed scope triage → candidate-only persistence."""

    def __init__(
        self,
        *,
        distiller: ToollessLessonDistiller,
        tenant_memory: TenantMemorySink,
        contributions: PriorContributionStore,
        general_candidates: GeneralCandidateSink,
        prior_candidates: PriorCandidateSink,
        prior_builder: PriorBuilder,
    ) -> None:
        if getattr(distiller, "tools_enabled", None) is not False:
            raise ValueError("lesson distiller must declare tools_enabled=False")
        self._distiller = distiller
        self._tenant_memory = tenant_memory
        self._contributions = contributions
        self._general_candidates = general_candidates
        self._prior_candidates = prior_candidates
        self._prior_builder = prior_builder

    def process(
        self,
        attribution: OutcomeAttribution,
        triage: ScopeTriageDecision,
        *,
        now: datetime | None = None,
        name_registry: Callable[[str], bool] | None = None,
    ) -> LearningResult:
        lesson = self._distiller.distill(attribution)
        if not isinstance(lesson, DistilledLesson):
            raise LearningRejected("distiller returned a non-DistilledLesson value")
        if lesson.raw_story_present:
            raise LearningRejected("distilled lesson still contains a raw tenant story")

        from orchestrator.knowledge.embeddings import redact_for_embedding

        safe_statement = redact_for_embedding(
            [lesson.abstract_statement], name_registry=name_registry
        )[0]
        created_at = now or datetime.now(UTC)
        if created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        if triage.scope is LearningScope.TENANT:
            assert triage.tenant_layer is not None
            self._tenant_memory.add(
                TenantLessonRecord(
                    tenant_id=attribution.tenant_id,
                    layer=triage.tenant_layer,
                    outcome_ref=attribution.outcome_ref,
                    statement=safe_statement,
                    mechanism_code=lesson.mechanism_code,
                    evidence_refs=attribution.evidence_refs,
                    created_at=created_at,
                )
            )
            return LearningResult(scope=triage.scope, destination=triage.tenant_layer.value)

        if triage.scope is LearningScope.GENERAL:
            if name_registry is None:
                raise LearningRejected(
                    "general lesson requires tenant name-registry redaction"
                )
            _assert_safe_abstract_statement(safe_statement, attribution.tenant_id)
            candidate_id = hashlib.sha256(
                f"{lesson.mechanism_code}|{safe_statement}".encode()
            ).hexdigest()
            self._general_candidates.add(
                GeneralLessonCandidate(
                    candidate_id=candidate_id,
                    statement=safe_statement,
                    mechanism_code=lesson.mechanism_code,
                    evidence_refs=tuple(_digest_ref(ref) for ref in attribution.evidence_refs),
                    created_at=created_at,
                )
            )
            return LearningResult(scope=triage.scope, destination="candidate_registry")

        if attribution.prior_key is None or attribution.normalized_outcome is None:
            raise LearningRejected("repeatable aggregate requires a structured prior observation")
        evidence_digest = hashlib.sha256(
            "|".join(sorted(attribution.evidence_refs)).encode()
        ).hexdigest()
        contribution = PriorContribution(
            contribution_id=hashlib.sha256(
                f"{attribution.tenant_id}|{attribution.outcome_ref}|{attribution.prior_key.canonical}".encode()
            ).hexdigest(),
            tenant_id=attribution.tenant_id,
            key=attribution.prior_key,
            normalized_outcome=attribution.normalized_outcome,
            occurred_at=attribution.occurred_at,
            captured_at=created_at,
            evidence_ref_digest=evidence_digest,
        )
        self._contributions.add(contribution)
        decision = self._prior_builder.assess(
            self._contributions.for_key(attribution.prior_key), now=created_at
        )
        if decision.disposition is PriorDisposition.CANDIDATE:
            assert decision.candidate is not None
            self._prior_candidates.add(decision.candidate)
        return LearningResult(
            scope=triage.scope,
            destination=(
                "l3_prior_candidate"
                if decision.disposition is PriorDisposition.CANDIDATE
                else "l3_prior_quarantine"
            ),
            prior_decision=decision,
        )


def _digest_ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _assert_safe_abstract_statement(statement: str, tenant_id: UUID) -> None:
    if str(tenant_id).casefold() in statement.casefold() or _UUIDISH_RE.search(statement):
        raise LearningRejected("global/learned statement contains a tenant-like identifier")
    if _REDACTION_TOKEN_RE.search(statement):
        raise LearningRejected("global/learned statement contains a unique redaction token")


class InMemoryTenantMemorySink:
    def __init__(self) -> None:
        self.records: list[TenantLessonRecord] = []

    def add(self, record: TenantLessonRecord) -> None:
        self.records.append(record)


class InMemoryPriorContributionStore:
    def __init__(self) -> None:
        self._records: dict[str, PriorContribution] = {}

    def add(self, contribution: PriorContribution) -> None:
        existing = self._records.get(contribution.contribution_id)
        if existing is not None and existing != contribution:
            raise LearningRejected("contribution idempotency collision")
        self._records[contribution.contribution_id] = contribution

    def for_key(self, key: PriorKey) -> Sequence[PriorContribution]:
        return tuple(item for item in self._records.values() if item.key == key)


class InMemoryGeneralCandidateSink:
    def __init__(self) -> None:
        self.candidates: list[GeneralLessonCandidate] = []

    def add(self, candidate: GeneralLessonCandidate) -> None:
        self.candidates.append(candidate)


class InMemoryPriorCandidateSink:
    def __init__(self) -> None:
        self.candidates: list[PriorCandidate] = []

    def add(self, candidate: PriorCandidate) -> None:
        if candidate not in self.candidates:
            self.candidates.append(candidate)


__all__ = [
    "DistilledLesson",
    "GeneralLessonCandidate",
    "InMemoryGeneralCandidateSink",
    "InMemoryPriorCandidateSink",
    "InMemoryPriorContributionStore",
    "InMemoryTenantMemorySink",
    "KAnonDecision",
    "K_ANON_FLOOR",
    "LearningLoop",
    "LearningRejected",
    "LearningResult",
    "LearningScope",
    "OutcomeAttribution",
    "PriorAdmissionDecision",
    "PriorAdmissionPolicy",
    "PriorBuilder",
    "PriorCandidate",
    "PriorContribution",
    "PriorDisposition",
    "PriorKey",
    "ScopeTriageDecision",
    "TenantLessonRecord",
    "ToollessLessonDistiller",
    "TriageAuthority",
    "vt225_k_anon_gate",
]
