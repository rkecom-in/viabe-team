"""VT-726 deterministic seed admission and GLOBAL registry persistence.

This module accepts artifacts produced by the real VT-710 ingestion pipeline. It does not extract
or hand-author cards. The seed validator proves structural/originality/purity properties and
promotes immutable candidate v1 rows to validated v2 rows for *shadow* retrieval. Corpus impact
admission remains pending until O11 and Fazal-approved graduation thresholds exist.

All retrieval output is advisory. Nothing in this module grants effect authorization.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from orchestrator.knowledge.contracts import CardStatus, KnowledgeCard, SourceClass
from orchestrator.knowledge_global_purity import assert_global_payload_pure


SEED_LEGACY_IDS = frozenset(
    {
        "bk035-decision-triage-impact-reversibility-uncertainty",
        "bk039-bias-and-incentive-distortion-check",
        "bk052-robust-decisions-under-deep-uncertainty",
        "bk056-dynamic-resource-allocation-portfolio",
        "bk062-batna-reservation-and-walkaway",
        "bk073-integrated-commercial-excellence-system",
        "bk077-customer-feedback-closed-loop",
        "bk078-structured-sales-peer-learning",
        "bk080-risk-bounded-online-experiment",
        "bk081-management-practices-as-operating-technology",
        "bk084-manager-allocation-under-customer-constraints",
        "bk086-quota-frequency-by-performer-and-product",
        "bk089-paid-digital-ads-amplify-readiness",
        "bk093-review-authenticity-and-manipulation-risk",
        "bk102-trade-credit-liquidity-shock",
    }
)

_REQUIRED_PIPELINE_STEPS = (
    "source_governance_recorded",
    "hashed_deduped",
    "raw_quarantined",
    "toolless_extracted",
    "schema_validated",
    "expression_originality_checked",
    "claim_applicability_normalized",
    "pii_redacted",
    "global_purity_checked",
    "independence_cluster_bound",
    "embedding_deferred_inert",
    "candidate_registered",
)
_VALIDATION_CHECKS = (
    "vt710_pipeline_complete",
    "schema_round_trip",
    "source_governance_present",
    "expression_originality_checked",
    "claim_key_canonical",
    "global_purity_rechecked",
    "no_review_flags",
    "t2_or_t3_seed_only",
)


class RegistrySeedError(ValueError):
    """A candidate or existing registry state cannot safely enter the seed."""


class ConnectionLike(Protocol):
    def execute(self, query: str, params: object = ...) -> Any: ...


@dataclass(frozen=True)
class SeedCard:
    legacy_id: str
    source: Mapping[str, Any]
    candidate: KnowledgeCard
    validated: KnowledgeCard
    pipeline_steps: tuple[str, ...]
    lifecycle_event_id: UUID
    lifecycle_reason: str


@dataclass(frozen=True)
class SeedRegistryPlan:
    """An idempotent shadow seed. It is evidence for the pipe, not O11 admission."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    corpus_version_id: UUID
    content_digest: str
    cards: tuple[SeedCard, ...]
    admission_verdict: str = "pending"
    corpus_status: str = "shadow"


def build_seed_plan(
    rights_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    *,
    tenant_identifiers: Sequence[str] = (),
) -> SeedRegistryPlan:
    """Select and deterministically validate the fixed 15-card seed from pipeline artifacts."""

    source_by_id = {str(row["source_id"]): row for row in rights_rows}
    selected = {str(row["legacy_id"]): row for row in candidates if row["legacy_id"] in SEED_LEGACY_IDS}
    missing = sorted(SEED_LEGACY_IDS - selected.keys())
    if missing:
        raise RegistrySeedError(f"seed candidates missing from VT-710 output: {missing}")
    if len(selected) != 15:
        raise RegistrySeedError("VT-726 seed must contain exactly 15 unique cards")

    validated_inputs: list[tuple[str, Mapping[str, Any], KnowledgeCard, tuple[str, ...]]] = []
    digest_payload: list[dict[str, Any]] = []
    for legacy_id in sorted(selected):
        artifact = selected[legacy_id]
        source_id = str(artifact["source_id"])
        source = source_by_id.get(source_id)
        if source is None:
            raise RegistrySeedError(f"{legacy_id}: source-governance row is missing")
        if source.get("paywall_access_circumvented") is True:
            raise RegistrySeedError(f"{legacy_id}: paywall circumvention is excluded")
        if source.get("contractual_extraction_restriction") is True:
            raise RegistrySeedError(f"{legacy_id}: contractual extraction restriction requires review")

        card = KnowledgeCard.model_validate(artifact["card"])
        steps = tuple(str(step) for step in artifact.get("pipeline_steps", ()))
        if steps != _REQUIRED_PIPELINE_STEPS:
            raise RegistrySeedError(f"{legacy_id}: incomplete or reordered VT-710 pipeline evidence")
        originality = artifact.get("expression_originality", {})
        if originality.get("mode") != "checked" or originality.get("scanner") != "token-shingle-v1":
            raise RegistrySeedError(f"{legacy_id}: source expression was not mechanically checked")
        if artifact.get("review_flags"):
            raise RegistrySeedError(f"{legacy_id}: unresolved source review flags")
        if card.status is not CardStatus.CANDIDATE or card.retrieval_eligible:
            raise RegistrySeedError(f"{legacy_id}: expected inert candidate v1")
        if card.source_class not in {SourceClass.T2_EVIDENCE, SourceClass.T3_PRACTITIONER}:
            raise RegistrySeedError(f"{legacy_id}: seed auto-validation is restricted to T2/T3")
        if card.claim_key.canonical != artifact["card"]["claim_key"].get("subject", "") + "|" + artifact["card"]["claim_key"].get("predicate", "") + "|" + artifact["card"]["claim_key"].get("jurisdiction", "") + "|" + artifact["card"]["claim_key"].get("population", "") + "|" + artifact["card"]["claim_key"].get("channel", ""):
            raise RegistrySeedError(f"{legacy_id}: claim key did not round-trip canonically")
        assert_global_payload_pure(
            card.model_dump(mode="json", exclude={"tenant_id"}),
            tenant_identifiers=tenant_identifiers,
        )
        validated_inputs.append((legacy_id, source, card, steps))
        digest_payload.append(
            {"legacy_id": legacy_id, "card": card.model_dump(mode="json"), "steps": steps}
        )

    content_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    corpus_id = uuid5(NAMESPACE_URL, f"viabe:o8:vt726:seed:{content_digest}")
    planned: list[SeedCard] = []
    for legacy_id, source, candidate, steps in validated_inputs:
        validated_version_id = uuid5(
            NAMESPACE_URL, f"viabe:o8:card-version:{legacy_id}:2"
        )
        validated = candidate.model_copy(
            update={
                "card_version_id": str(validated_version_id),
                "card_version": 2,
                "corpus_version_id": str(corpus_id),
                "status": CardStatus.VALIDATED,
                "retrieval_eligible": True,
            }
        )
        # model_copy intentionally avoids validation; force the immutable result back through it.
        validated = KnowledgeCard.model_validate(validated.model_dump(mode="json"))
        reason = json.dumps(
            {
                "gate": "vt726-deterministic-shadow-validation",
                "pipeline_steps": steps,
                "checks": _VALIDATION_CHECKS,
                "corpus_admission": "pending_o11_and_fazal_thresholds",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        planned.append(
            SeedCard(
                legacy_id=legacy_id,
                source=source,
                candidate=candidate,
                validated=validated,
                pipeline_steps=steps,
                lifecycle_event_id=uuid5(
                    NAMESPACE_URL, f"viabe:o8:vt726:promotion:{validated_version_id}"
                ),
                lifecycle_reason=reason,
            )
        )
    return SeedRegistryPlan(corpus_version_id=corpus_id, content_digest=content_digest, cards=tuple(planned))


def persist_seed_plan(conn: ConnectionLike, plan: SeedRegistryPlan) -> None:
    """Persist candidates, immutable promotions, provenance and shadow corpus atomically.

    The caller owns the transaction. Inserts are deterministic and idempotent; conflicting
    pre-existing version/corpus identities still fail through database constraints.
    """

    if plan.admission_verdict != "pending" or plan.corpus_status != "shadow":
        raise RegistrySeedError("VT-726 may persist only a shadow corpus with pending admission")

    for item in plan.cards:
        source = item.source
        conn.execute(
            "INSERT INTO public.knowledge_sources "
            "(id, canonical_url, publisher, source_class, content_hash, acquired_at, usage_rights, "
            " retention_class, tainted, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                source["source_id"], source["canonical_url"], source["publisher"],
                source["source_class"], source["content_hash"], source["acquired_at"],
                _json(source["usage_rights"]), source["retention_class"], source["tainted"],
                source.get("expires_at"),
            ),
        )
        _insert_card(conn, item.candidate, supersedes_card_id=None)
        _insert_source_edge(conn, item.candidate, str(source["source_id"]))

    conn.execute(
        "INSERT INTO public.knowledge_corpus_versions "
        "(id, version, content_digest, status, admission_verdict, created_by) "
        "VALUES (%s, 1, %s, 'shadow', 'pending', 'vt726-seed-pipeline') "
        "ON CONFLICT (id) DO NOTHING",
        (plan.corpus_version_id, plan.content_digest),
    )

    for item in plan.cards:
        _insert_card(conn, item.validated, supersedes_card_id=item.candidate.card_version_id)
        _insert_source_edge(conn, item.validated, str(item.source["source_id"]))
        conn.execute(
            "INSERT INTO public.knowledge_lifecycle_events "
            "(id, card_id, card_version_ref, event_type, from_status, to_status, actor_id, reason, "
            " idempotency_key) VALUES "
            "(%s, %s, %s, 'promotion', 'candidate', 'validated', "
            " 'vt726-deterministic-validator', %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                item.lifecycle_event_id,
                item.validated.card_version_id,
                item.validated.card_version_id,
                item.lifecycle_reason,
                f"vt726:promote:{item.validated.card_version_id}",
            ),
        )
        conn.execute(
            "INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id) "
            "VALUES (%s, %s) ON CONFLICT (corpus_version_id, card_id) DO NOTHING",
            (plan.corpus_version_id, item.validated.card_version_id),
        )


def load_validated_cards(conn: ConnectionLike, corpus_version_id: UUID) -> tuple[KnowledgeCard, ...]:
    """Reconstruct retrieval-ready cards with one read from ``knowledge_cards`` only."""

    rows = conn.execute(
        "SELECT id, card_key, version, corpus_version_id, claim, claim_key, claim_value, "
        "distillation_note, source_class, domain, authority, confidence, independence_cluster, "
        "corroboration_cluster_count, jurisdictions, size_bands, industries, maturity_stages, "
        "channels, applicability_universal, effective_from, effective_until, provenance, "
        "usage_rights, retention_class, scope, default_assignment, status, retrieval_eligible, "
        "expires_at FROM public.knowledge_cards "
        "WHERE corpus_version_id = %s AND status = 'validated' AND retrieval_eligible = true "
        "ORDER BY id",
        (corpus_version_id,),
    ).fetchall()
    return tuple(_card_from_row(row) for row in rows)


def _insert_card(conn: ConnectionLike, card: KnowledgeCard, *, supersedes_card_id: str | None) -> None:
    conn.execute(
        "INSERT INTO public.knowledge_cards "
        "(id, card_key, version, corpus_version_id, claim, claim_key, claim_value, "
        " distillation_note, source_class, domain, authority, confidence, independence_cluster, "
        " corroboration_cluster_count, jurisdictions, size_bands, industries, maturity_stages, "
        " channels, applicability_universal, effective_from, effective_until, provenance, "
        " usage_rights, retention_class, scope, default_assignment, status, retrieval_eligible, "
        " expires_at, tainted, supersedes_card_id) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        " %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            card.card_version_id, card.card_id, card.card_version, card.corpus_version_id,
            card.claim, card.claim_key.canonical, _json(card.claim_value.model_dump(mode="json")),
            card.distillation_note, card.source_class.value, card.domain.value,
            card.authority.value, card.confidence.value, card.independence_cluster,
            card.corroboration_cluster_count, list(card.applicability.jurisdictions),
            list(card.applicability.size_bands), list(card.applicability.industries),
            list(card.applicability.maturity_stages), list(card.applicability.channels),
            card.applicability.universal, card.applicability.effective_from,
            card.applicability.effective_to, _json(card.provenance.model_dump(mode="json")),
            _json(card.usage_rights.model_dump(mode="json")), card.retention_class,
            card.scope.value, card.default_assignment, card.status.value,
            card.retrieval_eligible, card.expires_at, card.provenance.tainted,
            supersedes_card_id,
        ),
    )


def _insert_source_edge(conn: ConnectionLike, card: KnowledgeCard, source_id: str) -> None:
    edge_id = uuid5(NAMESPACE_URL, f"viabe:o8:edge:{card.card_version_id}:{source_id}")
    conn.execute(
        "INSERT INTO public.knowledge_card_sources "
        "(id, card_id, source_id, independence_cluster_id) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (card_id, source_id) DO NOTHING",
        (edge_id, card.card_version_id, source_id, card.independence_cluster),
    )


def _card_from_row(row: Mapping[str, Any]) -> KnowledgeCard:
    claim_key = str(row["claim_key"]).split("|")
    if len(claim_key) != 5:
        raise RegistrySeedError(f"stored claim key is not canonical: {row['claim_key']!r}")
    return KnowledgeCard.model_validate(
        {
            "card_id": str(row["card_key"]),
            "card_version_id": str(row["id"]),
            "card_version": row["version"],
            "corpus_version_id": str(row["corpus_version_id"]),
            "claim": row["claim"],
            "claim_key": dict(zip(("subject", "predicate", "jurisdiction", "population", "channel"), claim_key, strict=True)),
            "claim_value": row["claim_value"],
            "distillation_note": row["distillation_note"],
            "source_class": row["source_class"],
            "domain": row["domain"],
            "authority": row["authority"],
            "confidence": row["confidence"],
            "independence_cluster": row["independence_cluster"],
            "corroboration_cluster_count": row["corroboration_cluster_count"],
            "applicability": {
                "jurisdictions": row["jurisdictions"],
                "size_bands": row["size_bands"],
                "industries": row["industries"],
                "maturity_stages": row["maturity_stages"],
                "channels": row["channels"],
                "universal": row["applicability_universal"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_until"],
            },
            "provenance": row["provenance"],
            "usage_rights": row["usage_rights"],
            "retention_class": row["retention_class"],
            "scope": row["scope"],
            "default_assignment": row["default_assignment"],
            "status": row["status"],
            "retrieval_eligible": row["retrieval_eligible"],
            "expires_at": row["expires_at"],
        }
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "RegistrySeedError",
    "SEED_LEGACY_IDS",
    "SeedCard",
    "SeedRegistryPlan",
    "build_seed_plan",
    "load_validated_cards",
    "persist_seed_plan",
]
