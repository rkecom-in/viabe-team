"""Typed contracts for the Phase-1 unified knowledge retrieval boundary.

The contracts are intentionally storage-agnostic. L1-L4, conversation, correction,
and task-state adapters can retain their existing physical stores while returning one
evidence shape to the broker. ``KnowledgeQuery`` carries no tenant identifier: tenancy
is supplied by trusted runtime context through ``KnowledgeScope``.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.knowledge_contracts import (
    ConflictManifestEntry,
    EvidenceManifestEntry,
    GroundingBehavior,
    GroundingStatus,
    KnowledgeDomain,
    KnowledgeLayer,
    KnowledgeVersion,
    NoResultBehavior,
    RetrievalDepth,
    RetrievalProfile,
    RetrievalStage,
)


class SpecialistName(StrEnum):
    ONBOARDING = "onboarding_conductor"
    INTEGRATION = "integration_agent"
    SALES_RECOVERY = "sales_recovery_agent"


class MemoryKind(StrEnum):
    FACT = "fact"
    RELATIONSHIP = "relationship"
    EPISODE = "episode"
    POLICY = "policy"
    DIRECTIVE = "directive"
    CORRECTION = "correction"
    OUTCOME = "outcome"
    SEED = "seed"
    TASK = "task"


class EvidenceAuthority(StrEnum):
    OWNER = "owner"
    VERIFIED_SYSTEM = "verified_system"
    VTR = "vtr"
    VERIFIED_OUTCOME = "verified_outcome"
    SEED = "seed"
    AGENT_INFERENCE = "agent_inference"


class EvidenceConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERIFIED = "verified"


class SourceClass(StrEnum):
    """Source character.  It is independent from authority and confidence."""

    T1_REGULATORY = "t1"
    T1_VENDOR_POLICY = "t1v"
    T2_EVIDENCE = "t2"
    T3_PRACTITIONER = "t3"
    T4_EXPERIENTIAL = "t4"


class CardStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    RESEARCH_ONLY = "research_only"
    EMERGENCY_QUARANTINED = "quarantined"


class KnowledgeScopeKind(StrEnum):
    GLOBAL = "global"
    PRIOR = "prior"
    TENANT = "tenant"


class UsageRightsStatus(StrEnum):
    OPEN_LICENSED = "open_licensed"
    PUBLIC_DOMAIN = "public_domain"
    PERMISSION_GRANTED = "permission_granted"
    LIVE_LINK_ONLY = "live_link_only"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class ClaimValueType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class CorpusVersionStatus(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    VALIDATED = "validated"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class UsageRights(BaseModel):
    """Deterministic source-rights decision made before any extraction or embedding."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    status: UsageRightsStatus
    license_id: str | None = Field(default=None, max_length=200)
    terms_url: str | None = Field(default=None, max_length=2_000)
    allows_extraction: bool = False
    allows_embedding: bool = False
    allows_retrieval: bool = False
    reviewed_at: datetime
    reviewed_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _rights_bound_capabilities(self) -> "UsageRights":
        if self.reviewed_at.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        if self.status in {
            UsageRightsStatus.LIVE_LINK_ONLY,
            UsageRightsStatus.RESTRICTED,
            UsageRightsStatus.UNKNOWN,
        } and (self.allows_extraction or self.allows_embedding or self.allows_retrieval):
            raise ValueError(f"rights status {self.status.value} cannot grant content use")
        if self.status is UsageRightsStatus.OPEN_LICENSED and not self.license_id:
            raise ValueError("open_licensed rights require license_id")
        return self


class ClaimKey(BaseModel):
    """Normalized identity used to decide whether two claims are comparable."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    jurisdiction: str = Field(min_length=1, max_length=100)
    population: str = Field(min_length=1, max_length=200)
    channel: str = Field(min_length=1, max_length=100)

    @field_validator("subject", "predicate", "jurisdiction", "population", "channel")
    @classmethod
    def _normalized_dimension(cls, value: str) -> str:
        normalized = "_".join(value.strip().lower().split())
        if any(char in normalized for char in ("|", "\n", "\r")):
            raise ValueError("claim-key dimensions cannot contain separators or newlines")
        return normalized

    @property
    def canonical(self) -> str:
        return "|".join(
            (self.subject, self.predicate, self.jurisdiction, self.population, self.channel)
        )


class TypedClaimValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    value_type: ClaimValueType
    value: str | int | Decimal | bool | date | datetime
    unit: str | None = Field(default=None, max_length=80)

    @field_validator("value_type", mode="before")
    @classmethod
    def _parse_value_type(cls, value: ClaimValueType | str) -> ClaimValueType:
        return value if isinstance(value, ClaimValueType) else ClaimValueType(value)

    @model_validator(mode="after")
    def _value_matches_declared_type(self) -> "TypedClaimValue":
        allowed: dict[ClaimValueType, tuple[type[Any], ...]] = {
            ClaimValueType.TEXT: (str,),
            ClaimValueType.INTEGER: (int,),
            ClaimValueType.DECIMAL: (Decimal,),
            ClaimValueType.BOOLEAN: (bool,),
            ClaimValueType.DATE: (date,),
            ClaimValueType.DATETIME: (datetime,),
        }
        actual = type(self.value)
        if actual not in allowed[self.value_type]:
            raise ValueError(
                f"claim value_type={self.value_type.value} does not match {actual.__name__}"
            )
        if isinstance(self.value, datetime) and self.value.utcoffset() is None:
            raise ValueError("datetime claim values must be timezone-aware")
        return self


class Applicability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    jurisdictions: tuple[str, ...] = ()
    size_bands: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    maturity_stages: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    universal: bool = False

    @model_validator(mode="after")
    def _window_and_universal_are_coherent(self) -> "Applicability":
        for name in ("effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if self.universal and any(
            (self.jurisdictions, self.size_bands, self.industries, self.maturity_stages, self.channels)
        ):
            raise ValueError("universal applicability cannot also declare explicit dimensions")
        return self


class CardProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_ids: tuple[str, ...] = Field(min_length=1)
    publisher: str = Field(min_length=1, max_length=300)
    retrieved_at: datetime
    tainted: bool = True

    @model_validator(mode="after")
    def _retrieval_time_is_aware(self) -> "CardProvenance":
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return self


class KnowledgeCard(BaseModel):
    """Atomic governed card.  Raw source text has no place in this retrieval-eligible model."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    card_id: str = Field(min_length=1, max_length=200)
    card_version_id: str = Field(min_length=1, max_length=200)
    card_version: int = Field(ge=1)
    corpus_version_id: str | None = Field(default=None, max_length=200)
    claim: str = Field(min_length=1, max_length=4_000)
    distillation_note: str = Field(min_length=1, max_length=4_000)
    claim_key: ClaimKey
    claim_value: TypedClaimValue
    source_class: SourceClass
    domain: KnowledgeDomain
    authority: EvidenceAuthority
    confidence: EvidenceConfidence
    independence_cluster: str = Field(min_length=1, max_length=200)
    corroboration_cluster_count: int = Field(default=1, ge=1)
    applicability: Applicability
    provenance: CardProvenance
    usage_rights: UsageRights
    retention_class: str = Field(min_length=1, max_length=100)
    scope: KnowledgeScopeKind
    tenant_id: UUID | None = Field(default=None, exclude=True)
    status: CardStatus = CardStatus.CANDIDATE
    retrieval_eligible: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _governance_invariants(self) -> "KnowledgeCard":
        if self.scope is KnowledgeScopeKind.TENANT and self.tenant_id is None:
            raise ValueError("tenant-scoped cards require tenant_id")
        if self.scope is not KnowledgeScopeKind.TENANT and self.tenant_id is not None:
            raise ValueError("global/k-gated cards cannot carry tenant_id")
        if self.source_class is SourceClass.T1_REGULATORY:
            if not self.applicability.jurisdictions or self.applicability.effective_from is None:
                raise ValueError("regulatory cards require jurisdiction and effective_from")
        if (
            self.source_class is SourceClass.T4_EXPERIENTIAL
            and self.status
            not in {
                CardStatus.CANDIDATE,
                CardStatus.RESEARCH_ONLY,
                CardStatus.EMERGENCY_QUARANTINED,
            }
            and self.corroboration_cluster_count < 2
        ):
            raise ValueError("T4 experiential cards cannot leave research-only without corroboration")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.source_class is SourceClass.T4_EXPERIENTIAL:
            max_expiry = _add_calendar_months(self.provenance.retrieved_at, 6)
            if self.expires_at is None or self.expires_at > max_expiry:
                raise ValueError("T4 experiential cards require auto-expiry within six months")
        if self.retrieval_eligible:
            if self.status not in {CardStatus.VALIDATED, CardStatus.DISPUTED}:
                raise ValueError("only validated/disputed cards may be retrieval eligible")
            if not self.usage_rights.allows_retrieval:
                raise ValueError("retrieval eligibility requires explicit retrieval rights")
        return self


def _add_calendar_months(value: datetime, months: int) -> datetime:
    """Add calendar months without pulling a date utility into the contracts layer."""

    target_month = value.month - 1 + months
    year = value.year + target_month // 12
    month = target_month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


_LIFECYCLE_TRANSITIONS: dict[CardStatus, frozenset[CardStatus]] = {
    CardStatus.CANDIDATE: frozenset(
        {CardStatus.VALIDATED, CardStatus.RESEARCH_ONLY, CardStatus.EMERGENCY_QUARANTINED}
    ),
    CardStatus.RESEARCH_ONLY: frozenset(
        {CardStatus.CANDIDATE, CardStatus.EMERGENCY_QUARANTINED, CardStatus.EXPIRED}
    ),
    CardStatus.VALIDATED: frozenset(
        {
            CardStatus.DISPUTED,
            CardStatus.SUPERSEDED,
            CardStatus.EXPIRED,
            CardStatus.EMERGENCY_QUARANTINED,
        }
    ),
    CardStatus.DISPUTED: frozenset(
        {
            CardStatus.VALIDATED,
            CardStatus.SUPERSEDED,
            CardStatus.EMERGENCY_QUARANTINED,
        }
    ),
    CardStatus.EMERGENCY_QUARANTINED: frozenset(
        {CardStatus.CANDIDATE, CardStatus.EXPIRED}
    ),
    CardStatus.SUPERSEDED: frozenset(),
    CardStatus.EXPIRED: frozenset(),
}


class CardLifecycleTransition(BaseModel):
    """Append-only, attributable and idempotent lifecycle mutation request."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    transition_id: UUID
    card_version_id: str = Field(min_length=1, max_length=200)
    from_status: CardStatus
    to_status: CardStatus
    reason: str = Field(min_length=1, max_length=2_000)
    actor_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    emergency: bool = False

    @model_validator(mode="after")
    def _transition_is_allowed(self) -> "CardLifecycleTransition":
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.to_status not in _LIFECYCLE_TRANSITIONS[self.from_status]:
            raise ValueError(
                f"invalid card lifecycle transition {self.from_status.value}->{self.to_status.value}"
            )
        if self.emergency and self.to_status is not CardStatus.EMERGENCY_QUARANTINED:
            raise ValueError("emergency transitions must target emergency_quarantined")
        return self


class CorpusVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    corpus_version_id: str = Field(min_length=1, max_length=200)
    parent_version_id: str | None = Field(default=None, max_length=200)
    status: CorpusVersionStatus = CorpusVersionStatus.DRAFT
    card_version_ids: tuple[str, ...]
    content_digest: str = Field(min_length=32, max_length=128)
    created_at: datetime
    created_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _created_at_is_aware(self) -> "CorpusVersion":
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if len(set(self.card_version_ids)) != len(self.card_version_ids):
            raise ValueError("card_version_ids must be unique")
        return self


def suggested_confidence_for_source(source_class: SourceClass) -> EvidenceConfidence:
    """A non-binding ingestion suggestion; callers must record confidence independently."""

    return {
        SourceClass.T1_REGULATORY: EvidenceConfidence.HIGH,
        SourceClass.T1_VENDOR_POLICY: EvidenceConfidence.HIGH,
        SourceClass.T2_EVIDENCE: EvidenceConfidence.HIGH,
        SourceClass.T3_PRACTITIONER: EvidenceConfidence.MEDIUM,
        SourceClass.T4_EXPERIENTIAL: EvidenceConfidence.LOW,
    }[source_class]


ALL_KNOWLEDGE_LAYERS = frozenset(KnowledgeLayer)
TENANT_SCOPED_LAYERS = frozenset(
    {
        KnowledgeLayer.L1,
        KnowledgeLayer.L2,
        KnowledgeLayer.CONVERSATION,
        KnowledgeLayer.CORRECTION,
        KnowledgeLayer.TASK,
    }
)
GLOBAL_LAYERS = frozenset({KnowledgeLayer.L3, KnowledgeLayer.L4})


class KnowledgeScope(BaseModel):
    """Trusted runtime scope. This object is never exposed as model tool input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    run_id: UUID


class KnowledgeQuery(BaseModel):
    """Model-safe retrieval request; deliberately has no ``tenant_id`` field."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    objective: str = Field(min_length=1, max_length=4_000)
    stage: RetrievalStage
    specialist: SpecialistName | None = None
    entity_refs: tuple[str, ...] = Field(default=(), max_length=50)
    time_horizon_days: int | None = Field(default=None, ge=1, le=3_650)
    token_budget: int = Field(default=2_500, ge=256, le=12_000)
    layers: frozenset[KnowledgeLayer] = Field(
        default=ALL_KNOWLEDGE_LAYERS, min_length=1
    )
    top_k_per_layer: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def _specialist_stage_has_specialist(self) -> KnowledgeQuery:
        if self.stage == RetrievalStage.SPECIALIST and self.specialist is None:
            raise ValueError("specialist is required for specialist-stage retrieval")
        return self


class EvidenceItem(BaseModel):
    """One provenance-bearing retrieval result.

    ``tenant_id`` is retained for broker-side isolation validation but excluded
    from serialization so it does not enter model context. Global L3/L4 evidence
    uses ``tenant_id=None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1, max_length=200)
    tenant_id: UUID | None = Field(default=None, exclude=True)
    layer: KnowledgeLayer
    kind: MemoryKind
    authority: EvidenceAuthority
    source_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=12_000)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    occurred_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: EvidenceConfidence = EvidenceConfidence.MEDIUM
    retrieval_eligible: bool = True
    superseded_by: str | None = None
    claim_key: str | None = Field(default=None, max_length=300)
    claim_value: str | None = Field(default=None, max_length=2_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validity_window_is_ordered(self) -> EvidenceItem:
        for name in ("occurred_at", "valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if (self.claim_key is None) != (self.claim_value is None):
            raise ValueError("claim_key and claim_value must be supplied together")
        return self


class KnowledgeConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_key: str
    evidence_ids: tuple[str, ...] = Field(min_length=2)
    claim_values: tuple[str, ...] = Field(min_length=2)


class RetrievalTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layers_queried: tuple[KnowledgeLayer, ...]
    layer_hits: dict[str, int]
    adapter_errors: dict[str, str]
    omitted_evidence_ids: tuple[str, ...]
    elapsed_ms: float = Field(ge=0.0)


class KnowledgeBundle(BaseModel):
    """Token-bounded evidence bundle returned to a reasoning-stage composer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: KnowledgeQuery
    facts: tuple[EvidenceItem, ...] = ()
    relationships: tuple[EvidenceItem, ...] = ()
    episodes: tuple[EvidenceItem, ...] = ()
    priors: tuple[EvidenceItem, ...] = ()
    policies_and_lessons: tuple[EvidenceItem, ...] = ()
    conflicts: tuple[KnowledgeConflict, ...] = ()
    evidence_manifest: tuple[str, ...] = ()
    token_count: int = Field(ge=0)
    trace: RetrievalTrace

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        grouped = (
            self.facts
            + self.relationships
            + self.episodes
            + self.priors
            + self.policies_and_lessons
        )
        by_id = {item.evidence_id: item for item in grouped}
        return tuple(by_id[evidence_id] for evidence_id in self.evidence_manifest)


__all__ = [
    "ALL_KNOWLEDGE_LAYERS",
    "Applicability",
    "CardLifecycleTransition",
    "CardProvenance",
    "CardStatus",
    "ClaimKey",
    "ClaimValueType",
    "ConflictManifestEntry",
    "CorpusVersion",
    "CorpusVersionStatus",
    "GLOBAL_LAYERS",
    "TENANT_SCOPED_LAYERS",
    "EvidenceAuthority",
    "EvidenceConfidence",
    "EvidenceManifestEntry",
    "EvidenceItem",
    "GroundingBehavior",
    "GroundingStatus",
    "KnowledgeBundle",
    "KnowledgeConflict",
    "KnowledgeCard",
    "KnowledgeDomain",
    "KnowledgeLayer",
    "KnowledgeQuery",
    "KnowledgeScope",
    "KnowledgeScopeKind",
    "KnowledgeVersion",
    "MemoryKind",
    "NoResultBehavior",
    "RetrievalDepth",
    "RetrievalProfile",
    "RetrievalStage",
    "RetrievalTrace",
    "SourceClass",
    "SpecialistName",
    "TypedClaimValue",
    "UsageRights",
    "UsageRightsStatus",
    "suggested_confidence_for_source",
]
