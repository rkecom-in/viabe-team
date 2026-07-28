"""VT-710 O8 rights-first, quarantined ingestion pipeline.

The pipeline is storage-agnostic and inert: callers supply acquisition, quarantine, dedupe,
embedding, and candidate-registry adapters. Raw content crosses only the acquisition/extraction
and quarantine boundaries; it is never placed on a ``KnowledgeCard`` or retrieval result.

The ordering is deliberate and testable:

    rights -> hash/dedupe -> quarantine -> tool-less extraction -> validation/normalization
    -> PII redaction -> independence cluster -> embedding -> candidate registry

Source character/rights come from ``SourceRightsDecision``; card-level metadata comes from trusted
``CandidateGovernance``. The extractor has no fields for source class, authority, confidence,
scope, or applicability, so prompt text cannot promote itself by asking the extractor to emit
privileged metadata.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.knowledge.contracts import (
    Applicability,
    CardProvenance,
    CardStatus,
    ClaimKey,
    ClaimValueType,
    EvidenceAuthority,
    EvidenceConfidence,
    KnowledgeCard,
    KnowledgeDomain,
    KnowledgeScopeKind,
    SourceClass,
    TypedClaimValue,
    UsageRights,
)


class IngestionRejected(RuntimeError):
    """A source failed a deterministic ingestion invariant."""


class DuplicateSource(IngestionRejected):
    """The exact acquired payload was already processed."""


class EmbeddingMode(StrEnum):
    REQUIRE = "require"
    DEFER = "defer"


class EmbeddingState(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"
    RIGHTS_BLOCKED = "rights_blocked"


class AcquiredContentKind(StrEnum):
    RAW_SOURCE = "raw_source"
    OWNED_DISTILLATION = "owned_distillation"


class AcquiredSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=200)
    canonical_url: str = Field(min_length=1, max_length=2_000)
    publisher: str = Field(min_length=1, max_length=300)
    acquired_at: datetime
    raw_text: str = Field(min_length=1)
    locator: str = Field(min_length=1, max_length=2_000)
    content_kind: AcquiredContentKind = AcquiredContentKind.RAW_SOURCE

    @model_validator(mode="after")
    def _acquired_at_is_aware(self) -> "AcquiredSource":
        if self.acquired_at.utcoffset() is None:
            raise ValueError("acquired_at must be timezone-aware")
        return self


class ExtractedClaimDraft(BaseModel):
    """The complete tool-less extraction output surface.

    Privileged source/governance fields are intentionally impossible to express here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    claim: str = Field(min_length=1, max_length=4_000)
    distillation_note: str = Field(min_length=1, max_length=4_000)
    claim_subject: str = Field(min_length=1, max_length=200)
    claim_predicate: str = Field(min_length=1, max_length=200)
    claim_value: TypedClaimValue


class SourceRightsDecision(BaseModel):
    """Source-level rights and character, resolved before any content processing."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_class: SourceClass
    usage_rights: UsageRights


class CandidateGovernance(BaseModel):
    """Trusted card-level metadata assigned outside source text and extractor output."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    domain: KnowledgeDomain
    authority: EvidenceAuthority
    confidence: EvidenceConfidence
    applicability: Applicability
    retention_class: str = Field(min_length=1, max_length=100)
    independence_cluster: str = Field(min_length=1, max_length=200)
    scope: KnowledgeScopeKind = KnowledgeScopeKind.GLOBAL
    corroboration_cluster_count: int = Field(default=1, ge=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _global_candidate_only(self) -> "CandidateGovernance":
        if self.scope is KnowledgeScopeKind.TENANT:
            raise ValueError("VT-710 external ingestion cannot construct tenant-scoped cards")
        return self


class QuarantineRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quarantine_ref: str
    source_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquired_at: datetime
    retrieval_eligible: Literal[False] = False


class CandidateArtifact(BaseModel):
    """A candidate plus non-content ingestion evidence; never a validated corpus member."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    card: KnowledgeCard
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quarantine_ref: str
    embedding_state: EmbeddingState
    embedding: tuple[float, ...] | None = Field(default=None, exclude=True)
    pipeline_steps: tuple[str, ...]

    @model_validator(mode="after")
    def _candidate_is_inert(self) -> "CandidateArtifact":
        if self.card.status not in {CardStatus.CANDIDATE, CardStatus.RESEARCH_ONLY}:
            raise ValueError("ingestion may write only candidate/research_only cards")
        if self.card.retrieval_eligible:
            raise ValueError("ingested candidates cannot be retrieval eligible")
        if self.embedding_state is EmbeddingState.COMPLETE and not self.embedding:
            raise ValueError("complete embedding_state requires an embedding")
        if self.embedding_state is not EmbeddingState.COMPLETE and self.embedding is not None:
            raise ValueError("non-complete embedding_state cannot carry an embedding")
        return self


class RightsResolver(Protocol):
    def resolve(self, source: AcquiredSource) -> SourceRightsDecision: ...


class ToollessExtractor(Protocol):
    tools_enabled: Literal[False]

    def extract(self, raw_text: str) -> ExtractedClaimDraft: ...


class QuarantineStore(Protocol):
    def put(self, source: AcquiredSource, *, content_hash: str) -> QuarantineRecord: ...


class DedupeStore(Protocol):
    def claim(self, content_hash: str) -> bool: ...


class CandidateRegistry(Protocol):
    def add(self, candidate: CandidateArtifact) -> None: ...


Embedder = Callable[[list[str]], Sequence[Sequence[float]]]


class InMemoryQuarantineStore:
    """Test/offline quarantine; raw reads are audit-only and never exposed as retrieval."""

    def __init__(self) -> None:
        self._records: dict[str, QuarantineRecord] = {}
        self._raw_for_audit: dict[str, str] = {}

    def put(self, source: AcquiredSource, *, content_hash: str) -> QuarantineRecord:
        ref = f"quarantine:{source.source_id}:{content_hash[:16]}"
        record = QuarantineRecord(
            quarantine_ref=ref,
            source_id=source.source_id,
            content_hash=content_hash,
            acquired_at=source.acquired_at,
        )
        self._records[ref] = record
        self._raw_for_audit[ref] = source.raw_text
        return record

    def read_for_audit(self, quarantine_ref: str) -> str:
        return self._raw_for_audit[quarantine_ref]


class InMemoryDedupeStore:
    def __init__(self) -> None:
        self._hashes: set[str] = set()

    def claim(self, content_hash: str) -> bool:
        if content_hash in self._hashes:
            return False
        self._hashes.add(content_hash)
        return True


class InMemoryCandidateRegistry:
    def __init__(self) -> None:
        self.candidates: list[CandidateArtifact] = []

    def add(self, candidate: CandidateArtifact) -> None:
        if candidate.card.retrieval_eligible:
            raise IngestionRejected("candidate registry rejects retrieval-eligible cards")
        self.candidates.append(candidate)


class MappingRightsResolver:
    """Exact source-id decisions; absence fails closed instead of inferring rights."""

    def __init__(self, decisions: Mapping[str, SourceRightsDecision]) -> None:
        self._decisions = dict(decisions)

    def resolve(self, source: AcquiredSource) -> SourceRightsDecision:
        try:
            return self._decisions[source.source_id]
        except KeyError as exc:
            raise IngestionRejected(
                f"no explicit rights/governance decision for source {source.source_id!r}"
            ) from exc


class IngestionPipeline:
    def __init__(
        self,
        *,
        rights: RightsResolver,
        quarantine: QuarantineStore,
        dedupe: DedupeStore,
        extractor: ToollessExtractor,
        registry: CandidateRegistry,
        embedder: Embedder | None,
        embedding_mode: EmbeddingMode = EmbeddingMode.REQUIRE,
        expected_embedding_dimensions: int = 1_024,
    ) -> None:
        if getattr(extractor, "tools_enabled", None) is not False:
            raise ValueError("ingestion extractor must declare tools_enabled=False")
        if embedding_mode is EmbeddingMode.REQUIRE and embedder is None:
            raise ValueError("required embedding mode needs an embedder")
        self._rights = rights
        self._quarantine = quarantine
        self._dedupe = dedupe
        self._extractor = extractor
        self._registry = registry
        self._embedder = embedder
        self._embedding_mode = embedding_mode
        self._expected_dimensions = expected_embedding_dimensions

    def ingest(
        self,
        source: AcquiredSource,
        *,
        governance: CandidateGovernance,
        card_id: str,
        card_version_id: str,
        card_version: int = 1,
        tenant_identifiers: Sequence[str] = (),
        name_registry: Callable[[str], bool] | None = None,
    ) -> CandidateArtifact:
        # Rights resolution is intentionally the first operation on acquired content.
        rights_decision = self._rights.resolve(source)
        steps = ["rights_verified"]

        if (
            source.content_kind is AcquiredContentKind.RAW_SOURCE
            and not rights_decision.usage_rights.allows_extraction
        ):
            raise IngestionRejected("source rights do not allow raw-content extraction")

        if rights_decision.source_class is SourceClass.T1_REGULATORY:
            if not governance.applicability.jurisdictions:
                raise IngestionRejected("T1 regulatory candidate requires jurisdiction")
            if governance.applicability.effective_from is None:
                raise IngestionRejected("T1 regulatory candidate requires effective_from")

        content_hash = hashlib.sha256(source.raw_text.encode("utf-8")).hexdigest()
        if not self._dedupe.claim(content_hash):
            raise DuplicateSource(f"duplicate source payload {content_hash}")
        steps.append("hashed_deduped")

        quarantine_record = self._quarantine.put(source, content_hash=content_hash)
        steps.append("raw_quarantined")

        draft = self._extractor.extract(source.raw_text)
        if not isinstance(draft, ExtractedClaimDraft):
            raise IngestionRejected("extractor returned a non-ExtractedClaimDraft value")
        steps.extend(("toolless_extracted", "schema_validated"))

        from orchestrator.knowledge.embeddings import redact_for_embedding

        text_value = (
            draft.claim_value.value
            if draft.claim_value.value_type is ClaimValueType.TEXT
            else ""
        )
        redacted_claim, redacted_note, redacted_subject, redacted_predicate, redacted_value = (
            redact_for_embedding(
                [
                    draft.claim,
                    draft.distillation_note,
                    draft.claim_subject,
                    draft.claim_predicate,
                    str(text_value),
                ],
                name_registry=name_registry,
            )
        )
        safe_claim_value = (
            draft.claim_value.model_copy(update={"value": redacted_value})
            if draft.claim_value.value_type is ClaimValueType.TEXT
            else draft.claim_value
        )
        claim_key = _normalized_claim_key(
            redacted_subject,
            redacted_predicate,
            governance.applicability,
        )
        steps.extend(("claim_applicability_normalized", "pii_redacted"))
        from orchestrator.knowledge_global_purity import assert_global_payload_pure

        assert_global_payload_pure(
            {
                "claim": redacted_claim,
                "distillation_note": redacted_note,
                "claim_key": claim_key.model_dump(mode="json"),
                "claim_value": safe_claim_value.model_dump(mode="json"),
            },
            tenant_identifiers=tenant_identifiers,
        )
        steps.append("global_purity_checked")

        status = (
            CardStatus.RESEARCH_ONLY
            if rights_decision.source_class is SourceClass.T4_EXPERIENTIAL
            else CardStatus.CANDIDATE
        )
        card = KnowledgeCard(
            card_id=card_id,
            card_version_id=card_version_id,
            card_version=card_version,
            claim=redacted_claim,
            distillation_note=redacted_note,
            claim_key=claim_key,
            claim_value=safe_claim_value,
            source_class=rights_decision.source_class,
            domain=governance.domain,
            authority=governance.authority,
            confidence=governance.confidence,
            independence_cluster=governance.independence_cluster,
            corroboration_cluster_count=governance.corroboration_cluster_count,
            applicability=governance.applicability,
            provenance=CardProvenance(
                source_ids=(source.source_id,),
                publisher=source.publisher,
                retrieved_at=source.acquired_at,
                tainted=True,
            ),
            usage_rights=rights_decision.usage_rights,
            retention_class=governance.retention_class,
            scope=governance.scope,
            status=status,
            retrieval_eligible=False,
            expires_at=governance.expires_at,
        )
        steps.append("independence_cluster_bound")

        embedding: tuple[float, ...] | None = None
        if not rights_decision.usage_rights.allows_embedding:
            embedding_state = EmbeddingState.RIGHTS_BLOCKED
            steps.append("embedding_rights_blocked")
        elif self._embedding_mode is EmbeddingMode.DEFER:
            embedding_state = EmbeddingState.PENDING
            steps.append("embedding_deferred_inert")
        else:
            assert self._embedder is not None
            embedded = self._embedder([f"{redacted_claim}\n{redacted_note}"])
            if len(embedded) != 1 or len(embedded[0]) != self._expected_dimensions:
                raise IngestionRejected(
                    "embedder must return exactly one vector with expected dimensions"
                )
            embedding = tuple(float(value) for value in embedded[0])
            embedding_state = EmbeddingState.COMPLETE
            steps.append("embedded")

        candidate = CandidateArtifact(
            card=card,
            source_content_hash=content_hash,
            quarantine_ref=quarantine_record.quarantine_ref,
            embedding_state=embedding_state,
            embedding=embedding,
            pipeline_steps=tuple((*steps, "candidate_registered")),
        )
        self._registry.add(candidate)
        return candidate


def _normalized_dimension(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if not normalized:
        raise IngestionRejected("claim-key dimension normalized to an empty value")
    return normalized


def _normalized_claim_key(
    claim_subject: str,
    claim_predicate: str,
    applicability: Applicability,
) -> ClaimKey:
    jurisdiction = (
        _normalized_dimension(applicability.jurisdictions[0])
        if applicability.jurisdictions
        else "unknown"
    )
    population = (
        _normalized_dimension(applicability.size_bands[0])
        if applicability.size_bands
        else "unknown"
    )
    channel = (
        _normalized_dimension(applicability.channels[0])
        if applicability.channels
        else "all"
    )
    return ClaimKey(
        subject=_normalized_dimension(claim_subject),
        predicate=_normalized_dimension(claim_predicate),
        jurisdiction=jurisdiction,
        population=population,
        channel=channel,
    )


__all__ = [
    "AcquiredSource",
    "AcquiredContentKind",
    "CandidateArtifact",
    "CandidateGovernance",
    "DuplicateSource",
    "EmbeddingMode",
    "EmbeddingState",
    "ExtractedClaimDraft",
    "InMemoryCandidateRegistry",
    "InMemoryDedupeStore",
    "InMemoryQuarantineStore",
    "IngestionPipeline",
    "IngestionRejected",
    "MappingRightsResolver",
    "QuarantineRecord",
    "SourceRightsDecision",
    "ToollessExtractor",
]
