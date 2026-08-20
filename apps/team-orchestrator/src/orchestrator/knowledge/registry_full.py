"""VT-727 full-corpus disposition, promotion, and GLOBAL registry persistence.

All 118 governed VT-710 artifacts enter this plan.  A record is either represented by a
shadow-validated immutable version or retained as an explicitly deferred candidate/research-only
version.  There is no silent-drop path.  The module never reads raw archived source expression and
never grants effect authority; retrieval remains advisory regardless of card status or score.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, ClassVar, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from orchestrator.knowledge.contracts import CardStatus, KnowledgeCard, SourceClass
from orchestrator.knowledge.registry_seed import (
    SEED_LEGACY_IDS,
    _insert_card,
    _insert_source_edge,
    _json,
    build_seed_plan,
)
from orchestrator.knowledge_global_purity import assert_global_payload_pure


_EXPECTED_RECORDS = 118
_EXPECTED_SOURCES = 104
_EXPECTED_PROMOTIONS = 64
_EXPECTED_DEFERRED = 54
_SCREEN_JACCARD = 0.14
_SCREEN_TITLE_RATIO = 0.56
_STOP_WORDS = frozenset(
    "the a an and or to of in for is are be this that with from on as it by into not should "
    "before when what how if than then".split()
)
_PIPELINE_PREFIX = (
    "source_governance_recorded",
    "hashed_deduped",
    "raw_quarantined",
    "toolless_extracted",
    "schema_validated",
)
_PIPELINE_SUFFIX = (
    "claim_applicability_normalized",
    "pii_redacted",
    "global_purity_checked",
    "independence_cluster_bound",
    "embedding_deferred_inert",
    "candidate_registered",
)


class FullRegistryError(ValueError):
    """The full corpus cannot safely or completely enter the governed registry."""


class ConnectionLike(Protocol):
    def execute(self, query: str, params: object = ...) -> Any: ...


@dataclass(frozen=True)
class IndependencePairReview:
    left_legacy_id: str
    right_legacy_id: str
    verdict: Literal["distinct", "same_underlying_evidence"]
    rationale_code: str

    @property
    def pair_key(self) -> tuple[str, str]:
        return tuple(sorted((self.left_legacy_id, self.right_legacy_id)))  # type: ignore[return-value]


@dataclass(frozen=True)
class IndependenceAudit:
    corpus_artifact_digest: str
    reviewed_record_count: int
    screening_jaccard: float
    screening_title_ratio: float
    reviewed_by: str
    reviewed_at: str
    pair_reviews: tuple[IndependencePairReview, ...]
    conclusion: Literal["retellings_collapsed", "no_cross_source_retellings_found"]


@dataclass(frozen=True)
class FullCardDisposition:
    legacy_id: str
    local_files: tuple[str, ...]
    source_id: str
    source_class: SourceClass
    original_status: CardStatus
    disposition: Literal["shadow_validated", "deferred_candidate", "deferred_research_only"]
    reasons: tuple[str, ...]
    route_out: tuple[str, ...]
    representative: KnowledgeCard
    candidate: KnowledgeCard
    source: Mapping[str, Any]
    lifecycle_event_id: UUID | None = None
    lifecycle_reason: str | None = None


@dataclass(frozen=True)
class FullRegistryPlan:
    """The complete, inert v2 corpus snapshot.  It authorizes no external effect."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    corpus_version_id: UUID
    parent_corpus_version_id: UUID
    content_digest: str
    cards: tuple[FullCardDisposition, ...]
    authority_counts: Mapping[str, int]
    largest_source_card_count: int
    largest_source_share: float
    screened_cross_source_pairs: int
    collapsed_retelling_groups: int
    corpus_status: str = "shadow"
    admission_verdict: str = "pending"

    @property
    def promoted_count(self) -> int:
        return sum(item.disposition == "shadow_validated" for item in self.cards)

    @property
    def deferred_count(self) -> int:
        return len(self.cards) - self.promoted_count


def load_independence_audit(payload: Mapping[str, Any]) -> IndependenceAudit:
    """Validate the human-review artifact without accepting free-form hidden defaults."""

    reviews_list: list[IndependencePairReview] = []
    for row in payload.get("pair_reviews", ()):
        verdict = str(row["verdict"])
        if verdict not in {"distinct", "same_underlying_evidence"}:
            raise FullRegistryError(f"invalid independence verdict: {verdict!r}")
        left, right = str(row["left_legacy_id"]), str(row["right_legacy_id"])
        rationale = str(row["rationale_code"]).strip()
        if left == right or not rationale:
            raise FullRegistryError("independence pair needs two records and a rationale")
        reviews_list.append(
            IndependencePairReview(
                left_legacy_id=left,
                right_legacy_id=right,
                verdict=verdict,  # type: ignore[arg-type]
                rationale_code=rationale,
            )
        )
    reviews = tuple(reviews_list)
    conclusion = str(payload["conclusion"])
    if conclusion not in {"retellings_collapsed", "no_cross_source_retellings_found"}:
        raise FullRegistryError(f"invalid independence conclusion: {conclusion!r}")
    return IndependenceAudit(
        corpus_artifact_digest=str(payload["corpus_artifact_digest"]),
        reviewed_record_count=int(payload["reviewed_record_count"]),
        screening_jaccard=float(payload["screening_jaccard"]),
        screening_title_ratio=float(payload["screening_title_ratio"]),
        reviewed_by=str(payload["reviewed_by"]),
        reviewed_at=str(payload["reviewed_at"]),
        pair_reviews=reviews,
        conclusion=conclusion,  # type: ignore[arg-type]
    )


def build_full_plan(
    rights_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    audit: IndependenceAudit,
    *,
    tenant_identifiers: Sequence[str] = (),
) -> FullRegistryPlan:
    """Create the deterministic 118-record plan after authority and independence review."""

    if len(candidates) != _EXPECTED_RECORDS or len(rights_rows) != _EXPECTED_SOURCES:
        raise FullRegistryError(
            f"VT-727 requires exactly {_EXPECTED_RECORDS} records/{_EXPECTED_SOURCES} sources"
        )
    legacy_ids = [str(row["legacy_id"]) for row in candidates]
    if len(set(legacy_ids)) != _EXPECTED_RECORDS:
        raise FullRegistryError("VT-727 candidate legacy IDs are not unique")

    artifact_digest = _artifact_digest(candidates)
    screened = screen_cross_source_pairs(candidates)
    _validate_independence_audit(audit, artifact_digest, screened)
    cluster_by_legacy = _clusters_after_audit(candidates, audit)

    source_by_id = {str(row["source_id"]): row for row in rights_rows}
    if len(source_by_id) != _EXPECTED_SOURCES:
        raise FullRegistryError("source-governance IDs are not unique")
    _assert_authority_classification(rights_rows)

    seed_plan = build_seed_plan(rights_rows, candidates, tenant_identifiers=tenant_identifiers)
    seed_validated = {item.legacy_id: item.validated for item in seed_plan.cards}
    disposition_inputs: list[tuple[Mapping[str, Any], Mapping[str, Any], KnowledgeCard, str]] = []
    digest_rows: list[dict[str, Any]] = []
    for artifact in sorted(candidates, key=lambda row: str(row["legacy_id"])):
        legacy_id = str(artifact["legacy_id"])
        source = source_by_id.get(str(artifact["source_id"]))
        if source is None:
            raise FullRegistryError(f"{legacy_id}: source-governance row missing")
        card = KnowledgeCard.model_validate(artifact["card"])
        _validate_pipeline_artifact(legacy_id, artifact, source, card, tenant_identifiers)
        cluster = cluster_by_legacy[legacy_id]
        if cluster != card.independence_cluster:
            card = KnowledgeCard.model_validate(
                card.model_copy(update={"independence_cluster": cluster}).model_dump(mode="json")
            )
        disposition_inputs.append((artifact, source, card, legacy_id))
        digest_rows.append(
            {
                "legacy_id": legacy_id,
                "card": card.model_dump(mode="json"),
                "pipeline_steps": artifact["pipeline_steps"],
                "audit_digest": audit.corpus_artifact_digest,
            }
        )

    content_digest = hashlib.sha256(
        json.dumps(digest_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    corpus_id = uuid5(NAMESPACE_URL, f"viabe:o8:vt727:full:{content_digest}")
    planned: list[FullCardDisposition] = []
    for artifact, source, candidate, legacy_id in disposition_inputs:
        checked = artifact["expression_originality"].get("mode") == "checked"
        promotable = (
            candidate.source_class in {SourceClass.T2_EVIDENCE, SourceClass.T3_PRACTITIONER}
            and checked
            and not artifact.get("review_flags")
        )
        if promotable:
            validated = seed_validated.get(legacy_id)
            if validated is None:
                validated = _promoted_card(candidate, legacy_id, corpus_id)
            reason = _promotion_reason(artifact)
            planned.append(
                FullCardDisposition(
                    legacy_id=legacy_id,
                    local_files=tuple(str(value) for value in source.get("local_files", ())),
                    source_id=str(source["source_id"]),
                    source_class=candidate.source_class,
                    original_status=candidate.status,
                    disposition="shadow_validated",
                    reasons=("deterministic_shadow_gate_passed",),
                    route_out=("o11_ablation_and_fazal_approved_graduation_thresholds",),
                    representative=validated,
                    candidate=candidate,
                    source=source,
                    lifecycle_event_id=(
                        None
                        if legacy_id in SEED_LEGACY_IDS
                        else uuid5(
                            NAMESPACE_URL,
                            f"viabe:o8:vt727:promotion:{validated.card_version_id}",
                        )
                    ),
                    lifecycle_reason=reason,
                )
            )
        else:
            reasons, route_out = _deferral(candidate, artifact)
            planned.append(
                FullCardDisposition(
                    legacy_id=legacy_id,
                    local_files=tuple(str(value) for value in source.get("local_files", ())),
                    source_id=str(source["source_id"]),
                    source_class=candidate.source_class,
                    original_status=candidate.status,
                    disposition=(
                        "deferred_research_only"
                        if candidate.status is CardStatus.RESEARCH_ONLY
                        else "deferred_candidate"
                    ),
                    reasons=reasons,
                    route_out=route_out,
                    representative=candidate,
                    candidate=candidate,
                    source=source,
                )
            )

    plan = FullRegistryPlan(
        corpus_version_id=corpus_id,
        parent_corpus_version_id=seed_plan.corpus_version_id,
        content_digest=content_digest,
        cards=tuple(planned),
        authority_counts={
            source_class.value: sum(item.source_class is source_class for item in planned)
            for source_class in SourceClass
        },
        largest_source_card_count=max(int(row["source_card_count"]) for row in rights_rows),
        largest_source_share=max(float(row["source_card_share"]) for row in rights_rows),
        screened_cross_source_pairs=len(screened),
        collapsed_retelling_groups=len(_retelling_components(audit)),
    )
    if plan.promoted_count != _EXPECTED_PROMOTIONS or plan.deferred_count != _EXPECTED_DEFERRED:
        raise FullRegistryError(
            f"measured gate drift: {plan.promoted_count} promoted/{plan.deferred_count} deferred"
        )
    if len(plan.cards) != _EXPECTED_RECORDS:
        raise FullRegistryError("silent drop detected while building the full plan")
    return plan


def persist_full_plan(conn: ConnectionLike, plan: FullRegistryPlan) -> None:
    """Persist all sources/cards/dispositions as a version-2 shadow snapshot, idempotently."""

    if plan.corpus_status != "shadow" or plan.admission_verdict != "pending":
        raise FullRegistryError("VT-727 persists only shadow/pending corpus state")
    if len(plan.cards) != _EXPECTED_RECORDS:
        raise FullRegistryError("refusing to persist an incomplete VT-727 plan")

    inserted_sources: set[str] = set()
    for item in plan.cards:
        source = item.source
        if item.source_id not in inserted_sources:
            conn.execute(
                "INSERT INTO public.knowledge_sources "
                "(id, canonical_url, publisher, source_class, content_hash, acquired_at, "
                " usage_rights, retention_class, tainted, expires_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    source["source_id"],
                    source["canonical_url"],
                    source["publisher"],
                    source["source_class"],
                    source["content_hash"],
                    source["acquired_at"],
                    _json(source["usage_rights"]),
                    source["retention_class"],
                    source["tainted"],
                    source.get("expires_at"),
                ),
            )
            inserted_sources.add(item.source_id)
        _insert_card(conn, item.candidate, supersedes_card_id=None)
        _insert_source_edge(conn, item.candidate, item.source_id)

    conn.execute(
        "INSERT INTO public.knowledge_corpus_versions "
        "(id, version, parent_corpus_version_id, content_digest, status, admission_verdict, "
        " created_by) VALUES (%s, 2, %s, %s, 'shadow', 'pending', 'vt727-full-pipeline') "
        "ON CONFLICT (id) DO NOTHING",
        (plan.corpus_version_id, plan.parent_corpus_version_id, plan.content_digest),
    )

    for item in plan.cards:
        if item.representative.card_version_id != item.candidate.card_version_id:
            _insert_card(
                conn,
                item.representative,
                supersedes_card_id=item.candidate.card_version_id,
            )
            _insert_source_edge(conn, item.representative, item.source_id)
        if item.lifecycle_event_id is not None:
            conn.execute(
                "INSERT INTO public.knowledge_lifecycle_events "
                "(id, card_id, card_version_ref, event_type, from_status, to_status, actor_id, "
                " reason, idempotency_key) VALUES "
                "(%s, %s, %s, 'promotion', 'candidate', 'validated', "
                " 'vt727-deterministic-validator', %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    item.lifecycle_event_id,
                    item.representative.card_version_id,
                    item.representative.card_version_id,
                    item.lifecycle_reason,
                    f"vt727:promote:{item.representative.card_version_id}",
                ),
            )
        conn.execute(
            "INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id) "
            "VALUES (%s, %s) ON CONFLICT (corpus_version_id, card_id) DO NOTHING",
            (plan.corpus_version_id, item.representative.card_version_id),
        )


def screen_cross_source_pairs(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    """Surface plausible retellings for explicit review; never auto-declare independence."""

    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if str(left["source_id"]) == str(right["source_id"]):
                continue
            left_tokens = _tokens(_card_text(left))
            right_tokens = _tokens(_card_text(right))
            jaccard = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
            title_ratio = SequenceMatcher(
                None, _title(str(left["legacy_id"])), _title(str(right["legacy_id"]))
            ).ratio()
            if jaccard >= _SCREEN_JACCARD or title_ratio >= _SCREEN_TITLE_RATIO:
                pairs.append(tuple(sorted((str(left["legacy_id"]), str(right["legacy_id"])))))
    return tuple(sorted(set(pairs)))


def _validate_pipeline_artifact(
    legacy_id: str,
    artifact: Mapping[str, Any],
    source: Mapping[str, Any],
    card: KnowledgeCard,
    tenant_identifiers: Sequence[str],
) -> None:
    steps = tuple(str(step) for step in artifact.get("pipeline_steps", ()))
    if (
        len(steps) != len(_PIPELINE_PREFIX) + 1 + len(_PIPELINE_SUFFIX)
        or steps[: len(_PIPELINE_PREFIX)] != _PIPELINE_PREFIX
        or steps[-len(_PIPELINE_SUFFIX) :] != _PIPELINE_SUFFIX
        or steps[len(_PIPELINE_PREFIX)]
        not in {"expression_originality_checked", "expression_originality_attested"}
    ):
        raise FullRegistryError(f"{legacy_id}: incomplete/reordered VT-710 pipeline evidence")
    if source.get("paywall_access_circumvented") is True:
        raise FullRegistryError(f"{legacy_id}: paywall circumvention is excluded")
    if card.status not in {CardStatus.CANDIDATE, CardStatus.RESEARCH_ONLY}:
        raise FullRegistryError(f"{legacy_id}: input must remain inert candidate/research-only")
    if card.retrieval_eligible:
        raise FullRegistryError(f"{legacy_id}: input unexpectedly retrieval eligible")
    assert_global_payload_pure(source, tenant_identifiers=tenant_identifiers)
    assert_global_payload_pure(
        card.model_dump(mode="json", exclude={"tenant_id"}),
        tenant_identifiers=tenant_identifiers,
    )


def _assert_authority_classification(rights_rows: Sequence[Mapping[str, Any]]) -> None:
    for source in rights_rows:
        source_types = {str(value).casefold() for value in source.get("source_type_inputs", ())}
        source_class = str(source["source_class"])
        if any("platform_guidance" in value for value in source_types) and source_class != "t4":
            raise FullRegistryError("platform guidance/community advice must be T4, never T1v")
        if any("platform_policy" in value for value in source_types) and source_class != "t1v":
            raise FullRegistryError("binding first-party platform policy must be T1v")


def _validate_independence_audit(
    audit: IndependenceAudit,
    artifact_digest: str,
    screened_pairs: Sequence[tuple[str, str]],
) -> None:
    if audit.corpus_artifact_digest != artifact_digest or audit.reviewed_record_count != 118:
        raise FullRegistryError("independence audit does not bind to this complete corpus artifact")
    if (
        audit.screening_jaccard != _SCREEN_JACCARD
        or audit.screening_title_ratio != _SCREEN_TITLE_RATIO
    ):
        raise FullRegistryError("independence audit used different screening thresholds")
    if not audit.reviewed_by.strip() or not audit.reviewed_at.strip():
        raise FullRegistryError("independence audit is not attributable")
    reviews = {review.pair_key: review for review in audit.pair_reviews}
    if set(reviews) != set(screened_pairs) or len(reviews) != len(audit.pair_reviews):
        raise FullRegistryError("every screened cross-source pair needs exactly one review")
    has_retelling = any(
        review.verdict == "same_underlying_evidence" for review in audit.pair_reviews
    )
    expected = "retellings_collapsed" if has_retelling else "no_cross_source_retellings_found"
    if audit.conclusion != expected:
        raise FullRegistryError("independence audit conclusion contradicts its pair verdicts")


def _clusters_after_audit(
    candidates: Sequence[Mapping[str, Any]], audit: IndependenceAudit
) -> dict[str, str]:
    clusters = {
        str(row["legacy_id"]): str(row["card"]["independence_cluster"]) for row in candidates
    }
    for component in _retelling_components(audit):
        cluster = "evidence:" + hashlib.sha256("|".join(component).encode()).hexdigest()[:24]
        for legacy_id in component:
            clusters[legacy_id] = cluster
    return clusters


def _retelling_components(audit: IndependenceAudit) -> tuple[tuple[str, ...], ...]:
    """Collapse transitive A↔B↔C retellings into one evidence cluster, not two pairs."""

    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for review in audit.pair_reviews:
        if review.verdict == "same_underlying_evidence":
            union(*review.pair_key)
    grouped: dict[str, set[str]] = {}
    for value in parent:
        grouped.setdefault(find(value), set()).add(value)
    return tuple(sorted(tuple(sorted(values)) for values in grouped.values()))


def _promoted_card(candidate: KnowledgeCard, legacy_id: str, corpus_id: UUID) -> KnowledgeCard:
    validated = candidate.model_copy(
        update={
            "card_version_id": str(uuid5(NAMESPACE_URL, f"viabe:o8:card-version:{legacy_id}:2")),
            "card_version": 2,
            "corpus_version_id": str(corpus_id),
            "status": CardStatus.VALIDATED,
            "retrieval_eligible": True,
        }
    )
    return KnowledgeCard.model_validate(validated.model_dump(mode="json"))


def _promotion_reason(artifact: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "gate": "vt727-deterministic-shadow-validation",
            "checks": [
                "vt710_pipeline_complete",
                "source_governance_present",
                "expression_originality_checked",
                "global_purity_rechecked",
                "authority_class_audited",
                "independence_pairs_reviewed",
                "t2_or_t3_only",
            ],
            "pipeline_steps": artifact["pipeline_steps"],
            "corpus_admission": "pending_o11_and_fazal_thresholds",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _deferral(
    card: KnowledgeCard, artifact: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasons: list[str] = []
    routes: list[str] = []
    if artifact["expression_originality"].get("mode") != "checked":
        reasons.append("originality_attestation_requires_independent_recheck")
        routes.append("run_mechanical_source_comparison_or_governed_original_author_review")
    if card.source_class is SourceClass.T1_REGULATORY:
        reasons.append("authoritative_effective_date_unverified")
        routes.append("verify_effective_period_against_authoritative_primary_text")
    if card.source_class is SourceClass.T1_VENDOR_POLICY:
        reasons.append("vendor_policy_currentness_requires_review")
        routes.append("verify_current_binding_vendor_policy_and_effective_period")
    if card.source_class is SourceClass.T4_EXPERIENTIAL:
        reasons.append("experiential_claim_requires_independent_corroboration")
        routes.append("obtain_two_genuinely_independent_evidence_clusters_or_keep_research_only")
    if not reasons:
        reasons.append("manual_accuracy_value_review_required")
        routes.append("complete_specialist_accuracy_and_value_review")
    return tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(routes))


def _artifact_digest(candidates: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(list(candidates), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _card_text(row: Mapping[str, Any]) -> str:
    return f"{row['card']['claim']}\n{row['card']['distillation_note']}"


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def _title(legacy_id: str) -> str:
    return legacy_id.split("-", 1)[-1]


__all__ = [
    "FullCardDisposition",
    "FullRegistryError",
    "FullRegistryPlan",
    "IndependenceAudit",
    "IndependencePairReview",
    "build_full_plan",
    "load_independence_audit",
    "persist_full_plan",
    "screen_cross_source_pairs",
]
