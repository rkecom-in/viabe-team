"""VT-723 evidence-bound resolution of the 18 T4 research-only cards.

The hunt changes evidence state, never card expression. A T4 card becomes an inert candidate only
after two new qualifying independence clusters corroborate it. A credible contradiction creates an
inert disputed version. Retrieval and every external effect remain disabled.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.knowledge.contracts import CardProvenance, CardStatus, KnowledgeCard, SourceClass
from orchestrator.knowledge.ingestion import CandidateArtifact
from orchestrator.knowledge.persisted_embeddings import card_content_digest
from orchestrator.knowledge.registry_resolution import ResolutionPlan
from orchestrator.knowledge.registry_seed import _insert_card, _insert_source_edge, _json
from orchestrator.knowledge_global_purity import assert_global_payload_pure

EXPECTED_T4_CARDS = 18
MIN_NEW_CLUSTERS = 2


class CorroborationError(ValueError):
    """The hunt artifact cannot safely change a T4 card's evidence state."""


class ConnectionLike(Protocol):
    def execute(self, query: str, params: object = ...) -> Any: ...


class EvidenceStance(StrEnum):
    CORROBORATES = "corroborates"
    REFUTES = "refutes"
    PARTIAL = "partial"


class ResolutionStatus(StrEnum):
    CANDIDATE = "candidate"
    DISPUTED = "disputed"
    RESEARCH_ONLY = "research_only"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceSupport(_StrictModel):
    legacy_id: str = Field(min_length=1)
    stance: EvidenceStance
    locator: str = Field(min_length=1)
    finding: str = Field(min_length=1, max_length=1_000)
    qualifies_for_threshold: bool

    @model_validator(mode="after")
    def _partial_never_counts(self) -> "SourceSupport":
        if self.stance is EvidenceStance.PARTIAL and self.qualifies_for_threshold:
            raise ValueError("partial evidence cannot satisfy the corroboration threshold")
        return self


class CorroborationSource(_StrictModel):
    source_id: str = Field(min_length=1)
    canonical_url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    source_class: Literal["t1", "t1v", "t2", "t3"]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquired_at: datetime
    local_archive_path: str = Field(min_length=1)
    local_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retention_class: Literal["local_source_reproduction"]
    usage_rights: Mapping[str, Any]
    paywall_access_circumvented: Literal[False]
    contractual_extraction_restriction: bool
    compilation_concentration: bool
    independence_cluster: str = Field(min_length=1)
    underlying_evidence_id: str = Field(min_length=1)
    depends_on_original_forum: Literal[False]
    candidate_card_version_id: str = Field(min_length=1)
    pipeline_steps: tuple[str, ...] = Field(min_length=1)
    originality_mode: Literal["checked"]
    originality_scanner: Literal["token-shingle-v1"]
    supports: tuple[SourceSupport, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _source_is_governed(self) -> "CorroborationSource":
        if self.acquired_at.utcoffset() is None:
            raise ValueError("acquired_at must be timezone-aware")
        if self.local_archive_path.startswith("apps/"):
            raise ValueError("raw reproductions must remain in the local-only archive")
        if self.contractual_extraction_restriction:
            raise ValueError("contract-restricted extraction requires separate judgment")
        return self


class EvidenceEdge(_StrictModel):
    source_id: str = Field(min_length=1)
    independence_cluster: str = Field(min_length=1)
    stance: EvidenceStance
    locator: str = Field(min_length=1)
    qualifies_for_threshold: bool


class SearchRecord(_StrictModel):
    queries: tuple[str, ...] = Field(min_length=1)
    skipped_paywalled_sources: tuple[str, ...] = ()
    semantic_retellings_collapsed: tuple[str, ...] = ()
    recorded_absence: bool
    note: str = Field(min_length=1)


class CorroborationDeltaRow(_StrictModel):
    legacy_id: str = Field(min_length=1)
    prior_status: Literal["research_only"]
    resolved_status: ResolutionStatus
    original_source_id: str = Field(min_length=1)
    original_independence_cluster: str = Field(min_length=1)
    evidence_edges: tuple[EvidenceEdge, ...]
    qualifying_new_cluster_count: int = Field(ge=0)
    total_independence_cluster_count: int = Field(ge=1)
    search: SearchRecord
    resolution_reason: str = Field(min_length=1)
    authorizes_effects: Literal[False]

    @model_validator(mode="after")
    def _counts_match_edges(self) -> "CorroborationDeltaRow":
        corroborating = {
            edge.independence_cluster
            for edge in self.evidence_edges
            if edge.qualifies_for_threshold and edge.stance is EvidenceStance.CORROBORATES
        }
        all_qualifying = {
            edge.independence_cluster
            for edge in self.evidence_edges
            if edge.qualifies_for_threshold
        }
        if self.original_independence_cluster in all_qualifying:
            raise ValueError("the original forum cluster cannot count as independent evidence")
        if len(corroborating) != self.qualifying_new_cluster_count:
            raise ValueError("qualifying corroboration count does not match evidence edges")
        if self.total_independence_cluster_count != 1 + len(all_qualifying):
            raise ValueError("total count must include the original forum cluster exactly once")
        if self.resolved_status is ResolutionStatus.CANDIDATE:
            if len(corroborating) < MIN_NEW_CLUSTERS:
                raise ValueError("candidate transition requires two new corroboration clusters")
            if any(edge.stance is EvidenceStance.REFUTES for edge in self.evidence_edges):
                raise ValueError("a refuted claim cannot be labelled merely candidate")
            if self.search.recorded_absence:
                raise ValueError("a resolved candidate cannot report evidence absence")
        elif self.resolved_status is ResolutionStatus.DISPUTED:
            stances = {edge.stance for edge in self.evidence_edges if edge.qualifies_for_threshold}
            if not {EvidenceStance.CORROBORATES, EvidenceStance.REFUTES} <= stances:
                raise ValueError("disputed transition requires qualifying support and refutation")
        elif not self.search.recorded_absence:
            raise ValueError("an unresolved research-only claim needs recorded absence")
        return self


@dataclass(frozen=True)
class PlannedTransition:
    legacy_id: str
    prior: KnowledgeCard
    resolved: KnowledgeCard
    edges: tuple[EvidenceEdge, ...]
    lifecycle_event_id: UUID
    lifecycle_reason: str


@dataclass(frozen=True)
class T4CorroborationPlan:
    """Complete version-4 shadow snapshot; evidence changes remain non-serving."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    corpus_version_id: UUID
    parent_corpus_version_id: UUID
    content_digest: str
    sources: tuple[CorroborationSource, ...]
    source_candidates: tuple[CandidateArtifact, ...]
    transitions: tuple[PlannedTransition, ...]
    members: tuple[KnowledgeCard, ...]
    unresolved_legacy_ids: tuple[str, ...]
    corpus_status: str = "shadow"
    admission_verdict: str = "pending"

    @property
    def candidate_count(self) -> int:
        return sum(item.resolved.status is CardStatus.CANDIDATE for item in self.transitions)

    @property
    def disputed_count(self) -> int:
        return sum(item.resolved.status is CardStatus.DISPUTED for item in self.transitions)


def load_source_manifest(rows: Sequence[Mapping[str, Any]]) -> tuple[CorroborationSource, ...]:
    try:
        parsed = tuple(CorroborationSource.model_validate(row) for row in rows)
    except (TypeError, ValueError) as exc:
        raise CorroborationError(f"invalid corroboration source manifest: {exc}") from exc
    if len({item.source_id for item in parsed}) != len(parsed):
        raise CorroborationError("corroboration source IDs must be unique")
    clusters = [item.independence_cluster for item in parsed]
    if len(set(clusters)) != len(clusters):
        raise CorroborationError(
            "one row per underlying evidence cluster is required; retellings must collapse"
        )
    return parsed


def load_delta(rows: Sequence[Mapping[str, Any]]) -> tuple[CorroborationDeltaRow, ...]:
    try:
        parsed = tuple(CorroborationDeltaRow.model_validate(row) for row in rows)
    except (TypeError, ValueError) as exc:
        raise CorroborationError(f"invalid T4 corroboration delta: {exc}") from exc
    if len(parsed) != EXPECTED_T4_CARDS or len({row.legacy_id for row in parsed}) != len(parsed):
        raise CorroborationError("delta must contain exactly 18 unique T4 rows")
    return parsed


def build_corroboration_plan(
    parent: ResolutionPlan,
    sources: Sequence[CorroborationSource],
    source_candidates: Sequence[CandidateArtifact],
    rows: Sequence[CorroborationDeltaRow],
    *,
    tenant_identifiers: Sequence[str] = (),
) -> T4CorroborationPlan:
    """Bind VT-710 artifacts to the delta and mint immutable, status-only versions."""

    row_by_id = {row.legacy_id: row for row in rows}
    t4_by_card_id = {
        card.card_id: card
        for card in parent.members
        if card.source_class is SourceClass.T4_EXPERIENTIAL
    }
    if len(row_by_id) != EXPECTED_T4_CARDS or len(t4_by_card_id) != EXPECTED_T4_CARDS:
        raise CorroborationError("parent/delta T4 population drift")

    source_by_id = {source.source_id: source for source in sources}
    candidate_by_version = {item.card.card_version_id: item for item in source_candidates}
    if set(candidate_by_version) != {source.candidate_card_version_id for source in sources}:
        raise CorroborationError("every new source must have exactly one VT-710 artifact")
    for source in sources:
        candidate = candidate_by_version[source.candidate_card_version_id]
        if candidate.card.source_class.value != source.source_class:
            raise CorroborationError(f"{source.source_id}: source class drift")
        if tuple(candidate.pipeline_steps) != source.pipeline_steps:
            raise CorroborationError(f"{source.source_id}: pipeline evidence drift")
        if candidate.card.independence_cluster != source.independence_cluster:
            raise CorroborationError(f"{source.source_id}: independence cluster drift")
        if candidate.card.retrieval_eligible:
            raise CorroborationError("new-source candidates must remain inert")

    originals: dict[str, KnowledgeCard] = {}
    for legacy_id in row_by_id:
        card_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:card:{legacy_id}"))
        try:
            originals[legacy_id] = t4_by_card_id[card_id]
        except KeyError as exc:
            raise CorroborationError(f"{legacy_id}: T4 card missing from parent") from exc

    for row in rows:
        prior = originals[row.legacy_id]
        if prior.status is not CardStatus.RESEARCH_ONLY or prior.retrieval_eligible:
            raise CorroborationError(f"{row.legacy_id}: expected inert research-only parent")
        if prior.provenance.source_ids[0] != row.original_source_id:
            raise CorroborationError(f"{row.legacy_id}: original source ID drift")
        if prior.independence_cluster != row.original_independence_cluster:
            raise CorroborationError(f"{row.legacy_id}: original cluster drift")
        for edge in row.evidence_edges:
            source = source_by_id.get(edge.source_id)
            if source is None:
                raise CorroborationError(f"{row.legacy_id}: evidence source missing")
            matches = [support for support in source.supports if support.legacy_id == row.legacy_id]
            if len(matches) != 1:
                raise CorroborationError(f"{row.legacy_id}: source support must appear once")
            support = matches[0]
            if (
                edge.independence_cluster != source.independence_cluster
                or edge.stance is not support.stance
                or edge.locator != support.locator
                or edge.qualifies_for_threshold != support.qualifies_for_threshold
            ):
                raise CorroborationError(f"{row.legacy_id}: edge/manifest mismatch")

    digest_payload = {
        "parent": str(parent.corpus_version_id),
        "sources": [
            item.model_dump(mode="json")
            for item in sorted(sources, key=lambda item: item.source_id)
        ],
        "delta": [
            item.model_dump(mode="json") for item in sorted(rows, key=lambda item: item.legacy_id)
        ],
    }
    content_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    corpus_id = uuid5(NAMESPACE_URL, f"viabe:o8:vt723:t4-corroboration:{content_digest}")

    member_by_id = {card.card_id: card for card in parent.members}
    transitions: list[PlannedTransition] = []
    unresolved: list[str] = []
    for row in sorted(rows, key=lambda item: item.legacy_id):
        prior = originals[row.legacy_id]
        if row.resolved_status is ResolutionStatus.RESEARCH_ONLY:
            unresolved.append(row.legacy_id)
            continue
        status = CardStatus(row.resolved_status.value)
        source_ids = tuple(
            dict.fromkeys(
                (*prior.provenance.source_ids, *(edge.source_id for edge in row.evidence_edges))
            )
        )
        provenance = CardProvenance.model_validate(
            prior.provenance.model_copy(update={"source_ids": source_ids}).model_dump(mode="json")
        )
        resolved = prior.model_copy(
            update={
                "card_version_id": str(
                    uuid5(NAMESPACE_URL, f"viabe:o8:card-version:{row.legacy_id}:2")
                ),
                "card_version": 2,
                "corpus_version_id": str(corpus_id),
                "status": status,
                "retrieval_eligible": False,
                "corroboration_cluster_count": row.total_independence_cluster_count,
                "provenance": provenance,
            }
        )
        resolved = KnowledgeCard.model_validate(resolved.model_dump(mode="json"))
        if card_content_digest(prior) != card_content_digest(resolved):
            raise CorroborationError(f"{row.legacy_id}: evidence update rewrote expression")
        assert_global_payload_pure(
            resolved.model_dump(mode="json", exclude={"tenant_id"}),
            tenant_identifiers=tenant_identifiers,
        )
        reason = json.dumps(
            {
                "gate": "vt723-t4-independent-corroboration",
                "resolution": row.model_dump(mode="json"),
                "retrieval": "disabled_pending_accuracy_value_impact_admission",
                "authorizes_effects": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        transitions.append(
            PlannedTransition(
                legacy_id=row.legacy_id,
                prior=prior,
                resolved=resolved,
                edges=row.evidence_edges,
                lifecycle_event_id=uuid5(
                    NAMESPACE_URL, f"viabe:o8:vt723:{resolved.card_version_id}"
                ),
                lifecycle_reason=reason,
            )
        )
        member_by_id[prior.card_id] = resolved

    plan = T4CorroborationPlan(
        corpus_version_id=corpus_id,
        parent_corpus_version_id=parent.corpus_version_id,
        content_digest=content_digest,
        sources=tuple(sources),
        source_candidates=tuple(source_candidates),
        transitions=tuple(transitions),
        members=tuple(member_by_id[key] for key in sorted(member_by_id)),
        unresolved_legacy_ids=tuple(unresolved),
    )
    if len(plan.members) != 118 or len(plan.transitions) != 16:
        raise CorroborationError("plan must retain 118 members and resolve exactly 16 T4 cards")
    if plan.candidate_count != 15 or plan.disputed_count != 1 or len(unresolved) != 2:
        raise CorroborationError("result drift from 15 candidate / 1 disputed / 2 unresolved")
    if any(
        card.source_class is SourceClass.T4_EXPERIENTIAL and card.retrieval_eligible
        for card in plan.members
    ):
        raise CorroborationError("T4 evidence-state changes must remain non-serving")
    return plan


def persist_corroboration_plan(conn: ConnectionLike, plan: T4CorroborationPlan) -> None:
    """Persist sources, inert pipeline candidates, immutable T4 versions, and real cluster edges."""

    if plan.corpus_status != "shadow" or plan.admission_verdict != "pending":
        raise CorroborationError("corroboration persists only shadow/pending state")
    for source in plan.sources:
        conn.execute(
            "INSERT INTO public.knowledge_sources "
            "(id, canonical_url, publisher, source_class, content_hash, acquired_at, usage_rights, "
            " retention_class, tainted, expires_at) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, FALSE, NULL) "
            "ON CONFLICT (id) DO NOTHING",
            (
                source.source_id,
                source.canonical_url,
                source.publisher,
                source.source_class,
                source.content_hash,
                source.acquired_at,
                _json(source.usage_rights),
                source.retention_class,
            ),
        )
    for artifact in plan.source_candidates:
        _insert_card(conn, artifact.card, supersedes_card_id=None)
        _insert_source_edge(
            conn,
            artifact.card,
            artifact.card.provenance.source_ids[0],
            independence_cluster=artifact.card.independence_cluster,
        )

    conn.execute(
        "INSERT INTO public.knowledge_corpus_versions "
        "(id, version, parent_corpus_version_id, content_digest, status, admission_verdict, created_by) "
        "VALUES (%s, 4, %s, %s, 'shadow', 'pending', 'vt723-t4-corroboration') "
        "ON CONFLICT (id) DO NOTHING",
        (plan.corpus_version_id, plan.parent_corpus_version_id, plan.content_digest),
    )
    for item in plan.transitions:
        _insert_card(conn, item.resolved, supersedes_card_id=item.prior.card_version_id)
        for edge in item.edges:
            _insert_source_edge(
                conn,
                item.resolved,
                edge.source_id,
                independence_cluster=edge.independence_cluster,
                supports=edge.stance is not EvidenceStance.REFUTES,
            )
        _insert_source_edge(
            conn,
            item.resolved,
            item.prior.provenance.source_ids[0],
            independence_cluster=item.prior.independence_cluster,
        )
        event_type = "dispute" if item.resolved.status is CardStatus.DISPUTED else "promotion"
        conn.execute(
            "INSERT INTO public.knowledge_lifecycle_events "
            "(id, card_id, card_version_ref, event_type, from_status, to_status, actor_id, reason, "
            " idempotency_key) VALUES (%s, %s, %s, %s, 'research_only', %s, "
            " 'vt723-corroboration-validator', %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                item.lifecycle_event_id,
                item.resolved.card_version_id,
                item.resolved.card_version_id,
                event_type,
                item.resolved.status.value,
                item.lifecycle_reason,
                f"vt723:t4:{item.resolved.card_version_id}",
            ),
        )
    for card in plan.members:
        conn.execute(
            "INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id) "
            "VALUES (%s, %s) ON CONFLICT (corpus_version_id, card_id) DO NOTHING",
            (plan.corpus_version_id, card.card_version_id),
        )


def copy_corroboration_embeddings(conn: ConnectionLike, plan: T4CorroborationPlan) -> None:
    """Copy the 16 unchanged v1 vectors inside Postgres; this performs no provider egress."""

    for item in plan.transitions:
        digest = card_content_digest(item.prior)
        if digest != card_content_digest(item.resolved):
            raise CorroborationError(f"{item.legacy_id}: embedding copy would be unsafe")
        conn.execute(
            "INSERT INTO public.knowledge_card_embeddings "
            "(card_id, embedding_model, embedding_dimensions, content_digest, embedding) "
            "SELECT %s, embedding_model, embedding_dimensions, %s, embedding "
            "FROM public.knowledge_card_embeddings WHERE card_id = %s AND content_digest = %s "
            "ON CONFLICT (card_id) DO NOTHING",
            (
                item.resolved.card_version_id,
                digest,
                item.prior.card_version_id,
                digest,
            ),
        )


__all__ = [
    "CorroborationDeltaRow",
    "CorroborationError",
    "CorroborationSource",
    "EvidenceEdge",
    "EvidenceStance",
    "ResolutionStatus",
    "T4CorroborationPlan",
    "build_corroboration_plan",
    "copy_corroboration_embeddings",
    "load_delta",
    "load_source_manifest",
    "persist_corroboration_plan",
]
