"""VT-710 O8 source-governed, quarantined ingestion pipeline.

The pipeline is storage-agnostic and inert: callers supply acquisition, quarantine, dedupe,
embedding, and candidate-registry adapters. Raw content crosses only the acquisition/extraction
and quarantine boundaries; it is never placed on a ``KnowledgeCard`` or retrieval result.

The ordering is deliberate and testable:

    source governance -> hash/dedupe -> quarantine -> tool-less extraction -> schema validation
    -> expression-originality gate -> normalization/PII redaction -> independence cluster
    -> embedding -> candidate registry

Source character/access facts come from ``SourceRightsDecision``; card-level metadata comes from trusted
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


class SourceReviewFlag(StrEnum):
    """Non-blocking source review signals; neither flag decides card admission."""

    CONTRACTUAL_EXTRACTION_RESTRICTION = "contractual_extraction_restriction"
    COMPILATION_CONCENTRATION = "compilation_database_concentration"


class ExpressionOriginalityMode(StrEnum):
    CHECKED = "checked"
    ATTESTED = "attested"


class ExpressionOriginalityEvidence(BaseModel):
    """Non-source evidence retained with the inert candidate artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    mode: ExpressionOriginalityMode
    scanner: str | None = Field(default=None, max_length=100)
    attested_by: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _mode_has_matching_evidence(self) -> "ExpressionOriginalityEvidence":
        if self.mode is ExpressionOriginalityMode.CHECKED and not self.scanner:
            raise ValueError("checked originality requires scanner identity")
        if self.mode is ExpressionOriginalityMode.ATTESTED and not self.attested_by:
            raise ValueError("attested originality requires an attributable reviewer")
        return self


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
    # Excluded from serialization so source expression can never leak into a candidate artifact.
    # RAW_SOURCE uses raw_text itself.  OWNED_DISTILLATION must supply either the underlying source
    # expression or an attributable originality attestation (for example a live-link-only source).
    expression_reference_text: str | None = Field(default=None, min_length=1, exclude=True)
    expression_originality_attested_by: str | None = Field(
        default=None, min_length=1, max_length=200, exclude=True
    )

    @model_validator(mode="after")
    def _acquired_at_is_aware(self) -> "AcquiredSource":
        if self.acquired_at.utcoffset() is None:
            raise ValueError("acquired_at must be timezone-aware")
        if (
            self.content_kind is AcquiredContentKind.OWNED_DISTILLATION
            and self.expression_reference_text is None
            and self.expression_originality_attested_by is None
        ):
            raise ValueError(
                "owned distillation requires source expression or originality attestation"
            )
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
    """Source-level character and access record, resolved before content processing.

    The historical name is retained for compatibility. Unknown licensing is metadata, never an
    embedding/retrieval gate. Contractual restrictions are surfaced for judgment; circumvention
    of paywalled access is the one fail-closed acquisition exclusion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source_class: SourceClass
    usage_rights: UsageRights
    contractual_extraction_restriction: bool = False
    paywall_access_circumvented: bool = False
    compilation_concentration: bool = False


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
    review_flags: tuple[SourceReviewFlag, ...] = ()
    expression_originality: ExpressionOriginalityEvidence

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
    """Exact source-id governance records; absence fails closed instead of inferring facts."""

    def __init__(self, decisions: Mapping[str, SourceRightsDecision]) -> None:
        self._decisions = dict(decisions)

    def resolve(self, source: AcquiredSource) -> SourceRightsDecision:
        try:
            return self._decisions[source.source_id]
        except KeyError as exc:
            raise IngestionRejected(
                f"no explicit source-governance decision for source {source.source_id!r}"
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
        # Source governance is intentionally the first operation on acquired content. Rights
        # metadata describes the source reproduction; it never authorizes or blocks card use.
        rights_decision = self._rights.resolve(source)
        steps = ["source_governance_recorded"]

        if rights_decision.paywall_access_circumvented:
            raise IngestionRejected("paywalled source obtained by access circumvention is excluded")
        review_flags: list[SourceReviewFlag] = []
        if rights_decision.contractual_extraction_restriction:
            review_flags.append(SourceReviewFlag.CONTRACTUAL_EXTRACTION_RESTRICTION)
        if rights_decision.compilation_concentration:
            review_flags.append(SourceReviewFlag.COMPILATION_CONCENTRATION)

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

        expression_reference = (
            source.expression_reference_text
            if source.expression_reference_text is not None
            else source.raw_text
            if source.content_kind is AcquiredContentKind.RAW_SOURCE
            else None
        )
        if expression_reference is not None:
            _assert_expression_original(draft, expression_reference)
            steps.append("expression_originality_checked")
            originality_evidence = ExpressionOriginalityEvidence(
                mode=ExpressionOriginalityMode.CHECKED,
                scanner="token-shingle-v1",
            )
        elif source.expression_originality_attested_by is not None:
            # Live-link-only inputs cannot be compared offline. The attributable attestation is
            # visible in the pipeline evidence and must be revisited during admission review.
            steps.append("expression_originality_attested")
            originality_evidence = ExpressionOriginalityEvidence(
                mode=ExpressionOriginalityMode.ATTESTED,
                attested_by=source.expression_originality_attested_by,
            )
        else:  # Defensive even though AcquiredSource validation already closes this path.
            raise IngestionRejected("expression originality has no evidence")

        from orchestrator.knowledge.embeddings import redact_for_embedding

        text_value = (
            draft.claim_value.value if draft.claim_value.value_type is ClaimValueType.TEXT else ""
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
        if self._embedding_mode is EmbeddingMode.DEFER:
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
            review_flags=tuple(review_flags),
            expression_originality=originality_evidence,
        )
        self._registry.add(candidate)
        return candidate


def _normalized_dimension(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if not normalized:
        raise IngestionRejected("claim-key dimension normalized to an empty value")
    return normalized


_EXPRESSION_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_EXPRESSION_WORDS = 12
_NEAR_VERBATIM_WINDOW = 5
_NEAR_VERBATIM_COVERAGE = 0.80


def _assert_expression_original(
    draft: ExtractedClaimDraft,
    source_expression: str,
) -> None:
    """Reject exact or near-verbatim source expression using deterministic token overlap.

    Facts and short stock phrases are deliberately below the threshold. A candidate segment is
    rejected when it contains a 12-word source run, or when at least 80% of its ordered five-word
    shingles occur in the source. This is a conservative scanner, not a legal conclusion; passing
    it does not validate truth, value, or impact.
    """

    source_words = _expression_words(source_expression)
    if len(source_words) < _MIN_EXPRESSION_WORDS:
        return
    exact_runs = {
        tuple(source_words[index : index + _MIN_EXPRESSION_WORDS])
        for index in range(len(source_words) - _MIN_EXPRESSION_WORDS + 1)
    }
    source_shingles = {
        tuple(source_words[index : index + _NEAR_VERBATIM_WINDOW])
        for index in range(len(source_words) - _NEAR_VERBATIM_WINDOW + 1)
    }
    text_value = (
        str(draft.claim_value.value) if draft.claim_value.value_type is ClaimValueType.TEXT else ""
    )
    segments = (draft.claim, draft.distillation_note, text_value)
    for segment in segments:
        for passage in _candidate_passages(segment):
            words = _expression_words(passage)
            if len(words) < _MIN_EXPRESSION_WORDS:
                continue
            if any(
                tuple(words[index : index + _MIN_EXPRESSION_WORDS]) in exact_runs
                for index in range(len(words) - _MIN_EXPRESSION_WORDS + 1)
            ):
                raise IngestionRejected(
                    "candidate contains verbatim or near-verbatim source expression"
                )
            candidate_shingles = [
                tuple(words[index : index + _NEAR_VERBATIM_WINDOW])
                for index in range(len(words) - _NEAR_VERBATIM_WINDOW + 1)
            ]
            coverage = sum(item in source_shingles for item in candidate_shingles) / len(
                candidate_shingles
            )
            if coverage >= _NEAR_VERBATIM_COVERAGE:
                raise IngestionRejected(
                    "candidate contains verbatim or near-verbatim source expression"
                )


def _expression_words(value: str) -> list[str]:
    return _EXPRESSION_WORD_RE.findall(value.casefold())


def _candidate_passages(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[\n.!?]+", value) if part.strip())


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
    channel = _normalized_dimension(applicability.channels[0]) if applicability.channels else "all"
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
    "ExpressionOriginalityEvidence",
    "ExpressionOriginalityMode",
    "ExtractedClaimDraft",
    "InMemoryCandidateRegistry",
    "InMemoryDedupeStore",
    "InMemoryQuarantineStore",
    "IngestionPipeline",
    "IngestionRejected",
    "MappingRightsResolver",
    "QuarantineRecord",
    "SourceRightsDecision",
    "SourceReviewFlag",
    "ToollessExtractor",
]
