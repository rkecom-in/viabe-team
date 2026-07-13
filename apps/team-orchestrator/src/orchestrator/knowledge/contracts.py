"""Typed contracts for the Phase-1 unified knowledge retrieval boundary.

The contracts are intentionally storage-agnostic. L1-L4, conversation, correction,
and task-state adapters can retain their existing physical stores while returning one
evidence shape to the broker. ``KnowledgeQuery`` carries no tenant identifier: tenancy
is supplied by trusted runtime context through ``KnowledgeScope``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeLayer(StrEnum):
    L1 = "l1"
    L2 = "l2"
    L3 = "l3"
    L4 = "l4"
    CONVERSATION = "conversation"
    CORRECTION = "correction"
    TASK = "task"


class RetrievalStage(StrEnum):
    TRIAGE = "triage"
    PLANNING = "planning"
    SPECIALIST = "specialist"
    REVIEW = "review"
    VERIFICATION = "verification"


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
    "GLOBAL_LAYERS",
    "TENANT_SCOPED_LAYERS",
    "EvidenceAuthority",
    "EvidenceConfidence",
    "EvidenceItem",
    "KnowledgeBundle",
    "KnowledgeConflict",
    "KnowledgeLayer",
    "KnowledgeQuery",
    "KnowledgeScope",
    "MemoryKind",
    "RetrievalStage",
    "RetrievalTrace",
    "SpecialistName",
]
