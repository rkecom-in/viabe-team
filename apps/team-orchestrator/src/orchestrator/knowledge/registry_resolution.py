"""VT-727 governed routes-out for the 36 non-T4 full-corpus deferrals.

The delta contains evidence and identifiers only. It cannot inject replacement card prose: every
resolved version is derived from the immutable VT-710 candidate that already passed quarantine,
schema, PII, global-purity, source-governance, and expression-originality handling. Retrieval stays
advisory and this module never grants effect authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.knowledge.contracts import (
    CardProvenance,
    CardStatus,
    KnowledgeCard,
    SourceClass,
)
from orchestrator.knowledge.persisted_embeddings import card_content_digest
from orchestrator.knowledge.registry_full import FullCardDisposition, FullRegistryPlan
from orchestrator.knowledge.registry_seed import _insert_card, _insert_source_edge
from orchestrator.knowledge_global_purity import assert_global_payload_pure


_EXPECTED_RESOLUTIONS = 36
_EXPECTED_FINAL_SHADOW_VALIDATED = 100
_EXPECTED_T4_DEFERRED = 18
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResolutionError(ValueError):
    """The deferral delta is incomplete, overbroad, or not sufficiently evidenced."""


class ConnectionLike(Protocol):
    def execute(self, query: str, params: object = ...) -> Any: ...


class _StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffectivePeriodEvidence(_StrictEvidence):
    source_url: str = Field(min_length=1)
    evidence_url: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    source_date_text: str = Field(min_length=1)
    effective_from: datetime
    effective_to: datetime | None = None
    date_precision: Literal["instant", "day", "month", "year"]
    normalization: Literal["exact_in_source", "start_of_source_period"]
    verified_by: str = Field(min_length=1)
    verified_at: datetime

    @model_validator(mode="after")
    def _aware_and_ordered(self) -> "EffectivePeriodEvidence":
        _require_aware(self.effective_from, "effective_from")
        _require_aware(self.verified_at, "verified_at")
        if self.effective_to is not None:
            _require_aware(self.effective_to, "effective_to")
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to precedes effective_from")
        if self.date_precision != "instant" and self.normalization != "start_of_source_period":
            raise ValueError("non-instant source dates must disclose start-of-period normalization")
        return self


class OriginalityResolutionEvidence(_StrictEvidence):
    mode: Literal["mechanical", "attestation_stands"]
    outcome: Literal["pass", "attestation_stands"]
    source_ref: str = Field(min_length=1)
    source_sha256: str | None = None
    scanner: Literal["token-shingle-v1"] | None = None
    rationale_code: str = Field(min_length=1)
    attested_by: str | None = None
    verified_by: str = Field(min_length=1)
    verified_at: datetime

    @model_validator(mode="after")
    def _mode_has_real_evidence(self) -> "OriginalityResolutionEvidence":
        _require_aware(self.verified_at, "verified_at")
        if self.mode == "mechanical":
            if (
                self.outcome != "pass"
                or self.scanner != "token-shingle-v1"
                or self.source_sha256 is None
                or not _SHA256_RE.fullmatch(self.source_sha256)
                or self.attested_by is not None
            ):
                raise ValueError("mechanical originality requires scanner, pass, and source hash")
        elif (
            self.outcome != "attestation_stands"
            or self.attested_by is None
            or self.scanner is not None
            or self.source_sha256 is not None
        ):
            raise ValueError("live-link originality must retain attributable attestation")
        return self


class VendorPolicyValidationEvidence(_StrictEvidence):
    canonical_url: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    binding_locator: str = Field(min_length=1)
    support_locators: tuple[str, ...] = Field(min_length=1)
    effective_date_status: Literal["published", "not_published_by_vendor"]
    effective_from: datetime | None = None
    verified_by: str = Field(min_length=1)
    verified_at: datetime

    @model_validator(mode="after")
    def _currentness_is_explicit(self) -> "VendorPolicyValidationEvidence":
        _require_aware(self.verified_at, "verified_at")
        if self.effective_from is not None:
            _require_aware(self.effective_from, "effective_from")
        if (self.effective_date_status == "published") != (self.effective_from is not None):
            raise ValueError("published effective-date status and effective_from disagree")
        return self


class ResolutionDeltaRow(_StrictEvidence):
    legacy_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_class: Literal["t1", "t1v", "t3"]
    prior_disposition: Literal["deferred_candidate"]
    resolved_disposition: Literal["shadow_validated"]
    cleared_reasons: tuple[str, ...] = Field(min_length=1)
    effective_period: EffectivePeriodEvidence | None = None
    originality: OriginalityResolutionEvidence | None = None
    vendor_policy: VendorPolicyValidationEvidence | None = None
    authorizes_effects: Literal[False]


@dataclass(frozen=True)
class ResolutionPromotion:
    legacy_id: str
    source_id: str
    candidate: KnowledgeCard
    validated: KnowledgeCard
    lifecycle_event_id: UUID
    lifecycle_reason: str


@dataclass(frozen=True)
class ResolutionPlan:
    """The complete v3 shadow snapshot after resolving every non-T4 VT-727 deferral."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    corpus_version_id: UUID
    parent_corpus_version_id: UUID
    content_digest: str
    promotions: tuple[ResolutionPromotion, ...]
    members: tuple[KnowledgeCard, ...]
    corpus_status: str = "shadow"
    admission_verdict: str = "pending"

    @property
    def shadow_validated_count(self) -> int:
        return sum(card.retrieval_eligible for card in self.members)

    @property
    def deferred_count(self) -> int:
        return len(self.members) - self.shadow_validated_count


def load_resolution_delta(rows: Sequence[Mapping[str, Any]]) -> tuple[ResolutionDeltaRow, ...]:
    """Parse a content-free evidence delta; unknown fields fail closed."""

    try:
        parsed = tuple(ResolutionDeltaRow.model_validate(row) for row in rows)
    except (TypeError, ValueError) as exc:
        raise ResolutionError(f"invalid deferral-resolution evidence: {exc}") from exc
    if len(parsed) != _EXPECTED_RESOLUTIONS:
        raise ResolutionError(f"expected {_EXPECTED_RESOLUTIONS} resolution rows")
    if len({row.legacy_id for row in parsed}) != len(parsed):
        raise ResolutionError("resolution legacy IDs must be unique")
    return parsed


def build_resolution_plan(
    full_plan: FullRegistryPlan,
    rows: Sequence[ResolutionDeltaRow],
    *,
    tenant_identifiers: Sequence[str] = (),
) -> ResolutionPlan:
    """Resolve all 36 candidate deferrals while leaving all 18 T4 rows untouched."""

    deferred = {
        item.legacy_id: item for item in full_plan.cards if item.disposition == "deferred_candidate"
    }
    if len(deferred) != _EXPECTED_RESOLUTIONS:
        raise ResolutionError("parent plan does not expose the measured 36 candidate deferrals")
    row_by_id = {row.legacy_id: row for row in rows}
    if set(row_by_id) != set(deferred) or len(row_by_id) != len(rows):
        raise ResolutionError("delta must resolve exactly the 36 candidate deferrals")

    canonical_rows = [
        row_by_id[legacy_id].model_dump(mode="json") for legacy_id in sorted(row_by_id)
    ]
    content_digest = hashlib.sha256(
        json.dumps(
            {
                "parent": str(full_plan.corpus_version_id),
                "resolutions": canonical_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    corpus_id = uuid5(NAMESPACE_URL, f"viabe:o8:vt727:routes-out:{content_digest}")

    promotions: list[ResolutionPromotion] = []
    member_by_legacy = {item.legacy_id: item.representative for item in full_plan.cards}
    for legacy_id in sorted(deferred):
        item = deferred[legacy_id]
        row = row_by_id[legacy_id]
        _validate_resolution_row(item, row)
        corrected = _apply_resolution(item.candidate, row)
        validated = corrected.model_copy(
            update={
                "card_version_id": str(
                    uuid5(NAMESPACE_URL, f"viabe:o8:card-version:{legacy_id}:2")
                ),
                "card_version": 2,
                "corpus_version_id": str(corpus_id),
                "status": CardStatus.VALIDATED,
                "retrieval_eligible": True,
            }
        )
        validated = KnowledgeCard.model_validate(validated.model_dump(mode="json"))
        if card_content_digest(validated) != card_content_digest(item.candidate):
            raise ResolutionError(f"{legacy_id}: routes-out must not rewrite card expression")
        assert_global_payload_pure(
            validated.model_dump(mode="json", exclude={"tenant_id"}),
            tenant_identifiers=tenant_identifiers,
        )
        lifecycle_reason = json.dumps(
            {
                "gate": "vt727-deferral-routes-out",
                "cleared_reasons": list(row.cleared_reasons),
                "evidence": row.model_dump(mode="json"),
                "corpus_admission": "pending_o11_and_fazal_thresholds",
                "authorizes_effects": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        promotion = ResolutionPromotion(
            legacy_id=legacy_id,
            source_id=item.source_id,
            candidate=item.candidate,
            validated=validated,
            lifecycle_event_id=uuid5(
                NAMESPACE_URL, f"viabe:o8:vt727:routes-out:{validated.card_version_id}"
            ),
            lifecycle_reason=lifecycle_reason,
        )
        promotions.append(promotion)
        member_by_legacy[legacy_id] = validated

    members = tuple(member_by_legacy[key] for key in sorted(member_by_legacy))
    plan = ResolutionPlan(
        corpus_version_id=corpus_id,
        parent_corpus_version_id=full_plan.corpus_version_id,
        content_digest=content_digest,
        promotions=tuple(promotions),
        members=members,
    )
    if len(plan.members) != 118 or len(plan.promotions) != _EXPECTED_RESOLUTIONS:
        raise ResolutionError("routes-out plan silently lost a corpus member")
    if (
        plan.shadow_validated_count != _EXPECTED_FINAL_SHADOW_VALIDATED
        or plan.deferred_count != _EXPECTED_T4_DEFERRED
    ):
        raise ResolutionError("routes-out count drift from 100 validated / 18 T4 deferred")
    if any(
        card.source_class is SourceClass.T4_EXPERIENTIAL and card.retrieval_eligible
        for card in plan.members
    ):
        raise ResolutionError("T4 corroboration deferrals must remain untouched")
    return plan


def persist_resolution_plan(conn: ConnectionLike, plan: ResolutionPlan) -> None:
    """Append the immutable v2 repairs and complete v3 shadow membership; never update in place."""

    if plan.corpus_status != "shadow" or plan.admission_verdict != "pending":
        raise ResolutionError("routes-out persists only shadow/pending corpus state")
    if len(plan.members) != 118 or len(plan.promotions) != _EXPECTED_RESOLUTIONS:
        raise ResolutionError("refusing to persist an incomplete routes-out plan")
    conn.execute(
        "INSERT INTO public.knowledge_corpus_versions "
        "(id, version, parent_corpus_version_id, content_digest, status, admission_verdict, "
        " created_by) VALUES (%s, 3, %s, %s, 'shadow', 'pending', "
        " 'vt727-deferral-routes-out') ON CONFLICT (id) DO NOTHING",
        (plan.corpus_version_id, plan.parent_corpus_version_id, plan.content_digest),
    )
    for item in plan.promotions:
        _insert_card(conn, item.validated, supersedes_card_id=item.candidate.card_version_id)
        _insert_source_edge(conn, item.validated, item.source_id)
        conn.execute(
            "INSERT INTO public.knowledge_lifecycle_events "
            "(id, card_id, card_version_ref, event_type, from_status, to_status, actor_id, "
            " reason, idempotency_key) VALUES "
            "(%s, %s, %s, 'promotion', 'candidate', 'validated', "
            " 'vt727-deferral-resolution-validator', %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                item.lifecycle_event_id,
                item.validated.card_version_id,
                item.validated.card_version_id,
                item.lifecycle_reason,
                f"vt727:routes-out:{item.validated.card_version_id}",
            ),
        )
    for card in plan.members:
        conn.execute(
            "INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id) "
            "VALUES (%s, %s) ON CONFLICT (corpus_version_id, card_id) DO NOTHING",
            (plan.corpus_version_id, card.card_version_id),
        )


def copy_resolution_embeddings(conn: ConnectionLike, plan: ResolutionPlan) -> None:
    """Reuse v1 vectors because the delta changes metadata only, never embedded card expression."""

    for item in plan.promotions:
        candidate_digest = card_content_digest(item.candidate)
        validated_digest = card_content_digest(item.validated)
        if candidate_digest != validated_digest:
            raise ResolutionError(f"{item.legacy_id}: embedding text changed; copy is unsafe")
        conn.execute(
            "INSERT INTO public.knowledge_card_embeddings "
            "(card_id, embedding_model, embedding_dimensions, content_digest, embedding) "
            "SELECT %s, embedding_model, embedding_dimensions, %s, embedding "
            "FROM public.knowledge_card_embeddings "
            "WHERE card_id = %s AND content_digest = %s "
            "ON CONFLICT (card_id) DO NOTHING",
            (
                item.validated.card_version_id,
                validated_digest,
                item.candidate.card_version_id,
                candidate_digest,
            ),
        )


def _validate_resolution_row(item: FullCardDisposition, row: ResolutionDeltaRow) -> None:
    if row.source_id != item.source_id or row.source_class != item.source_class.value:
        raise ResolutionError(f"{row.legacy_id}: source identity/class drift")
    expected = set(item.reasons)
    if set(row.cleared_reasons) != expected or len(row.cleared_reasons) != len(expected):
        raise ResolutionError(f"{row.legacy_id}: every prior deferral reason must be cleared once")
    evidence_present = {
        name
        for name, value in (
            ("authoritative_effective_date_unverified", row.effective_period),
            ("originality_attestation_requires_independent_recheck", row.originality),
            ("vendor_policy_currentness_requires_review", row.vendor_policy),
        )
        if value is not None
    }
    if evidence_present != expected:
        raise ResolutionError(f"{row.legacy_id}: evidence does not exactly match prior reasons")
    canonical_url = str(item.source["canonical_url"])
    if row.effective_period is not None and row.effective_period.source_url != canonical_url:
        raise ResolutionError(f"{row.legacy_id}: effective-period source URL drift")
    if row.vendor_policy is not None and row.vendor_policy.canonical_url != canonical_url:
        raise ResolutionError(f"{row.legacy_id}: vendor-policy source URL drift")
    if item.source_class is SourceClass.T4_EXPERIENTIAL:
        raise ResolutionError(f"{row.legacy_id}: T4 is outside this delta")


def _apply_resolution(candidate: KnowledgeCard, row: ResolutionDeltaRow) -> KnowledgeCard:
    updates: dict[str, object] = {}
    applicability = candidate.applicability
    if row.effective_period is not None:
        applicability = applicability.model_copy(
            update={
                "effective_from": row.effective_period.effective_from,
                "effective_to": row.effective_period.effective_to,
            }
        )
    if row.vendor_policy is not None:
        # Clear the conversion-time observation date when the publisher exposes no effective
        # date. Keeping that timestamp would turn crawler observation into false policy authority.
        applicability = applicability.model_copy(
            update={"effective_from": row.vendor_policy.effective_from, "effective_to": None}
        )
        provenance = candidate.provenance.model_copy(
            update={"retrieved_at": row.vendor_policy.verified_at}
        )
        updates["provenance"] = CardProvenance.model_validate(provenance.model_dump(mode="json"))
    updates["applicability"] = applicability
    corrected = candidate.model_copy(update=updates)
    return KnowledgeCard.model_validate(corrected.model_dump(mode="json"))


def _require_aware(value: datetime, field: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


__all__ = [
    "EffectivePeriodEvidence",
    "OriginalityResolutionEvidence",
    "ResolutionDeltaRow",
    "ResolutionError",
    "ResolutionPlan",
    "ResolutionPromotion",
    "VendorPolicyValidationEvidence",
    "build_resolution_plan",
    "copy_resolution_embeddings",
    "load_resolution_delta",
    "persist_resolution_plan",
]
