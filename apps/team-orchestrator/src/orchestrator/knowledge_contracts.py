"""Dependency-free O8 knowledge and grounding contracts.

This module deliberately uses only the standard library.  The agent-framework manifest and
``ModuleResult`` are collected by the dependency-less smoke suite, while the storage-facing O8
models use Pydantic.  Keeping the small values shared by both sides here prevents either framework
contract from importing the heavier storage layer.

The evidence envelopes contain REFERENCES and normalized claim identity only.  They have no field
for source text, model prompts, customer facts, or tenant identity, so carrying one through a DBOS
or audit envelope cannot create a new raw-content/PII retention surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


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


class KnowledgeDomain(StrEnum):
    MANAGEMENT = "management"
    SALES = "sales"
    MARKETING = "marketing"
    COMPLIANCE = "compliance"
    FINANCE = "finance"
    ACCOUNTING = "accounting"
    OPERATIONS = "operations"
    ONBOARDING = "onboarding"
    INTEGRATION = "integration"
    TECHNOLOGY = "technology"
    COST_OPTIMIZATION = "cost_optimization"
    CROSS_FUNCTIONAL = "cross_functional"


class RetrievalDepth(StrEnum):
    """The Manager synthesizes conclusions; specialists may retrieve domain-deep material."""

    CONCLUSIONS = "conclusions"
    DOMAIN_DEEP = "domain_deep"


class GroundingBehavior(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    OPTIONAL = "optional"


class NoResultBehavior(StrEnum):
    HEDGE = "hedge"
    DECLINE = "decline"
    CONTINUE_WITHOUT_KNOWLEDGE = "continue_without_knowledge"


class GroundingStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    GROUNDED = "grounded"
    PARTIALLY_GROUNDED = "partially_grounded"
    UNGROUNDED = "ungrounded"
    DISPUTED = "disputed"


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class EvidenceManifestEntry:
    """A content-free pointer to one claim-bearing evidence item."""

    evidence_id: str
    source_id: str
    claim_key: str
    authority: str
    confidence: str
    independence_cluster: str
    card_version_id: str | None = None
    corpus_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "evidence_id",
            "source_id",
            "claim_key",
            "authority",
            "confidence",
            "independence_cluster",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        for name in ("card_version_id", "corpus_version_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required(value, name))


@dataclass(frozen=True)
class ConflictManifestEntry:
    """A disputed normalized claim; values/text intentionally do not ride the audit envelope."""

    claim_key: str
    evidence_ids: tuple[str, ...]
    status: str = "disputed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_key", _required(self.claim_key, "claim_key"))
        normalized = tuple(_required(value, "evidence_ids") for value in self.evidence_ids)
        if len(set(normalized)) < 2:
            raise ValueError("conflict evidence_ids must contain at least two distinct ids")
        object.__setattr__(self, "evidence_ids", normalized)
        if self.status != "disputed":
            raise ValueError("conflict status must be 'disputed'")


@dataclass(frozen=True)
class KnowledgeVersion:
    """The exact corpus/schema version used for a grounded result."""

    corpus_version_id: str
    registry_schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "corpus_version_id", _required(self.corpus_version_id, "corpus_version_id")
        )
        object.__setattr__(
            self,
            "registry_schema_version",
            _required(self.registry_schema_version, "registry_schema_version"),
        )


@dataclass(frozen=True)
class RetrievalProfile:
    """Declared retrieval capability and budget for one Manager/specialist identity."""

    identity: str
    domains: frozenset[KnowledgeDomain]
    layers: frozenset[KnowledgeLayer]
    stages: frozenset[RetrievalStage]
    top_k: int
    token_budget: int
    allow_disputed: bool
    depth: RetrievalDepth
    grounding_behavior: GroundingBehavior
    no_result_behavior: NoResultBehavior
    minimum_score: float = 0.5

    def validate(self) -> None:
        _required(self.identity, "retrieval_profile.identity")
        if not self.domains or any(not isinstance(v, KnowledgeDomain) for v in self.domains):
            raise ValueError("retrieval_profile.domains must be non-empty KnowledgeDomain values")
        if not self.layers or any(not isinstance(v, KnowledgeLayer) for v in self.layers):
            raise ValueError("retrieval_profile.layers must be non-empty KnowledgeLayer values")
        if not self.stages or any(not isinstance(v, RetrievalStage) for v in self.stages):
            raise ValueError("retrieval_profile.stages must be non-empty RetrievalStage values")
        if not 1 <= self.top_k <= 20:
            raise ValueError("retrieval_profile.top_k must be between 1 and 20")
        if not 256 <= self.token_budget <= 12_000:
            raise ValueError("retrieval_profile.token_budget must be between 256 and 12000")
        if not 0.0 <= self.minimum_score <= 1.0:
            raise ValueError("retrieval_profile.minimum_score must be between 0 and 1")
        if self.identity == "team_manager" and self.depth is not RetrievalDepth.CONCLUSIONS:
            raise ValueError("team_manager retrieval depth must be conclusions")


def grounding_audit_payload(
    *,
    evidence_manifest: tuple[EvidenceManifestEntry, ...],
    conflict_manifest: tuple[ConflictManifestEntry, ...],
    knowledge_version: KnowledgeVersion | None,
    grounding_status: GroundingStatus,
) -> dict[str, Any]:
    """Serialize the content-free provenance subset suitable for ``tm_audit.decision``."""

    return {
        "evidence_manifest": [asdict(item) for item in evidence_manifest],
        "conflict_manifest": [asdict(item) for item in conflict_manifest],
        "knowledge_version": asdict(knowledge_version) if knowledge_version is not None else None,
        "grounding_status": grounding_status.value,
    }


def validate_grounding_envelope(
    *,
    evidence_manifest: tuple[EvidenceManifestEntry, ...],
    conflict_manifest: tuple[ConflictManifestEntry, ...],
    knowledge_version: KnowledgeVersion | None,
    grounding_status: GroundingStatus,
) -> None:
    """Fail closed on contradictory grounding metadata at any adapter seam."""

    if not isinstance(grounding_status, GroundingStatus):
        raise ValueError("grounding_status must be a GroundingStatus value")
    if any(not isinstance(item, EvidenceManifestEntry) for item in evidence_manifest):
        raise ValueError("evidence_manifest must contain EvidenceManifestEntry values")
    if any(not isinstance(item, ConflictManifestEntry) for item in conflict_manifest):
        raise ValueError("conflict_manifest must contain ConflictManifestEntry values")
    if knowledge_version is not None and not isinstance(knowledge_version, KnowledgeVersion):
        raise ValueError("knowledge_version must be a KnowledgeVersion or None")
    if grounding_status in {GroundingStatus.GROUNDED, GroundingStatus.PARTIALLY_GROUNDED}:
        if not evidence_manifest or knowledge_version is None:
            raise ValueError(
                f"grounding_status={grounding_status.value} requires evidence and knowledge_version"
            )
    if grounding_status is GroundingStatus.DISPUTED and (
        not evidence_manifest or not conflict_manifest or knowledge_version is None
    ):
        raise ValueError("grounding_status=disputed requires evidence, conflicts, and knowledge_version")
    evidence_ids = {item.evidence_id for item in evidence_manifest}
    if len(evidence_ids) != len(evidence_manifest):
        raise ValueError("evidence_manifest contains duplicate evidence_id values")
    for conflict in conflict_manifest:
        missing = set(conflict.evidence_ids) - evidence_ids
        if missing:
            raise ValueError(f"conflict_manifest references absent evidence ids: {sorted(missing)!r}")


__all__ = [
    "ConflictManifestEntry",
    "EvidenceManifestEntry",
    "GroundingBehavior",
    "GroundingStatus",
    "KnowledgeDomain",
    "KnowledgeLayer",
    "KnowledgeVersion",
    "NoResultBehavior",
    "RetrievalDepth",
    "RetrievalProfile",
    "RetrievalStage",
    "grounding_audit_payload",
    "validate_grounding_envelope",
]
