"""VT-710 inert O8 card retrieval policy engine and legacy-broker adapter.

Nothing imports or registers this adapter on a live route.  The engine operates on already-loaded
``KnowledgeCard`` values and enforces the O8 order before returning any content: scope/status,
hard applicability, hybrid scoring, claim-scoped authority, cluster dedupe/diversity, budgets,
explicit conflicts, and minimum-score no-result behavior.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.knowledge.contracts import (
    CardStatus,
    EvidenceConfidence,
    EvidenceItem,
    KnowledgeCard,
    KnowledgeDomain,
    KnowledgeLayer,
    KnowledgeQuery,
    KnowledgeScope,
    KnowledgeScopeKind,
    MemoryKind,
    RetrievalDepth,
    RetrievalProfile,
    SourceClass,
    validate_assignment_scope,
)
from orchestrator.knowledge_contracts import KnowledgeAssignmentScope

_WORD_RE = re.compile(r"[a-z0-9]+")
_CHARS_PER_TOKEN = 4
_ITEM_OVERHEAD_TOKENS = 12

_SOURCE_AUTHORITY = {
    SourceClass.T1_REGULATORY: 1.0,
    SourceClass.T1_VENDOR_POLICY: 0.9,
    SourceClass.T2_EVIDENCE: 0.8,
    SourceClass.T3_PRACTITIONER: 0.6,
    SourceClass.T4_EXPERIENTIAL: 0.3,
}
#: VT-725 — the hybrid weight vector.  ``RetrievalProfile.minimum_score`` is an EMPIRICAL floor
#: fitted against exactly these weights and the metrics below them (cosine semantic, Jaccard
#: lexical/entity).  Changing any weight, or any component's metric, moves the whole score scale and
#: silently invalidates that floor — so `test_o8_floor_calibration.py` pins this vector and fails
#: with a pointer to the re-derivation procedure rather than letting the floor rot unnoticed.
SCORE_WEIGHTS: dict[str, float] = {
    "semantic": 0.38,
    "lexical": 0.24,
    "entity": 0.10,
    "authority": 0.12,
    "applicability": 0.08,
    "confidence": 0.05,
    "recency": 0.03,
}

_CONFIDENCE = {
    EvidenceConfidence.LOW: 0.25,
    EvidenceConfidence.MEDIUM: 0.5,
    EvidenceConfidence.HIGH: 0.75,
    EvidenceConfidence.VERIFIED: 1.0,
}


class CardRetrievalPolicyError(RuntimeError):
    """A request violated declared retrieval scope/capability."""


class RetrievalBusinessContext(BaseModel):
    """Trusted tenant context. tenant_id is used for isolation but never serialized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID = Field(exclude=True)
    jurisdiction: str = Field(min_length=1, max_length=100)
    size_band: str | None = Field(default=None, max_length=100)
    industry: str | None = Field(default=None, max_length=200)
    maturity_stage: str | None = Field(default=None, max_length=100)
    channel: str | None = Field(default=None, max_length=100)
    as_of: datetime

    @model_validator(mode="after")
    def _as_of_is_aware(self) -> "RetrievalBusinessContext":
        if self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return self


@dataclass(frozen=True)
class ScoreComponents:
    semantic: float
    lexical: float
    #: VT-725 — ``None`` when the QUERY supplied no entity references at all, so there is nothing
    #: for a card to match against and the dimension does not exist for this turn.  Scoring that
    #: 0.0 docked every card equally for a fact about the query.
    entity: float | None
    authority: float
    applicability: float
    #: VT-736 — ``None`` means the dimension does not APPLY to this card (an evergreen claim does
    #: not age), which is different from 0.0 meaning "stale". The score renormalizes over the
    #: applicable dimensions rather than folding an inapplicable one in as a zero.
    recency: float | None
    confidence: float


@dataclass(frozen=True)
class RetrievedCard:
    card: KnowledgeCard
    score: float
    components: ScoreComponents
    content: str
    hedge_reasons: tuple[str, ...]
    estimated_tokens: int


@dataclass(frozen=True)
class RetrievedConflict:
    claim_key: str
    card_version_ids: tuple[str, ...]
    claim_values: tuple[str, ...]


@dataclass(frozen=True)
class CardRetrievalTrace:
    considered: int
    scope_or_status_excluded: int
    applicability_excluded: int
    below_score_excluded: int
    cluster_deduplicated: int
    diversity_or_budget_excluded: int
    elapsed_ms: float


@dataclass(frozen=True)
class CardRetrievalResult:
    """Advisory evidence for reasoning; it never authorizes an external effect."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    items: tuple[RetrievedCard, ...]
    conflicts: tuple[RetrievedConflict, ...]
    no_result: bool
    no_result_behavior: str
    trace: CardRetrievalTrace


@dataclass(frozen=True)
class CardAssignmentOverride:
    """Trusted tenant override loaded under RLS; never accepted from model input."""

    card_version_id: str
    tenant_id: UUID
    scope: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.card_version_id.strip():
            raise ValueError("card_version_id must be non-empty")
        validate_assignment_scope(self.scope)


class CardRetrievalEngine:
    """Pure in-memory policy engine; persistence/search backends remain replaceable."""

    def retrieve(
        self,
        *,
        cards: Sequence[KnowledgeCard],
        card_embeddings: Mapping[str, Sequence[float]],
        objective: str,
        query_embedding: Sequence[float] | None,
        entity_refs: Sequence[str],
        domain: KnowledgeDomain,
        stage: str,
        profile: RetrievalProfile,
        context: RetrievalBusinessContext,
        allowed_scopes: frozenset[KnowledgeScopeKind],
        assignment_overrides: Mapping[str, CardAssignmentOverride] | None = None,
        top_k: int | None = None,
        token_budget: int | None = None,
        max_per_cluster: int = 2,
    ) -> CardRetrievalResult:
        started = time.perf_counter()
        profile.validate()
        if domain not in profile.domains:
            raise CardRetrievalPolicyError(
                f"profile {profile.identity!r} does not declare domain {domain.value!r}"
            )
        if stage not in {value.value for value in profile.stages}:
            raise CardRetrievalPolicyError(
                f"profile {profile.identity!r} does not declare stage {stage!r}"
            )
        if not allowed_scopes or KnowledgeScopeKind.TENANT in allowed_scopes:
            raise CardRetrievalPolicyError("global card retrieval cannot include tenant scope")
        if max_per_cluster not in {1, 2}:
            raise CardRetrievalPolicyError("max_per_cluster must be 1 or 2")
        if query_embedding is None:
            raise CardRetrievalPolicyError("hybrid retrieval requires a query embedding")

        effective_top_k = min(top_k or profile.top_k, profile.top_k)
        effective_budget = min(token_budget or profile.token_budget, profile.token_budget)
        trusted_overrides = assignment_overrides or {}
        objective_tokens = _tokens(objective)
        entity_tokens = (
            set().union(*(_tokens(value) for value in entity_refs)) if entity_refs else set()
        )

        scope_or_status_excluded = 0
        applicability_excluded = 0
        below_score_excluded = 0
        scored: list[RetrievedCard] = []

        for card in cards:
            # Assignment is an advisory-context boundary, never effect authority. A tenant flip
            # can remove or redirect a card instantly, but cannot approve a send/spend/consent act.
            assignment = card.default_assignment
            override = trusted_overrides.get(card.card_version_id)
            if override is not None:
                if override.tenant_id != context.tenant_id:
                    raise CardRetrievalPolicyError(
                        "assignment override tenant did not match retrieval context"
                    )
                assignment = (
                    override.scope if override.enabled else KnowledgeAssignmentScope.DISABLED.value
                )

            # 1-2. Assignment, scope and lifecycle exclusions happen before ranking.
            if (
                assignment == KnowledgeAssignmentScope.DISABLED.value
                or assignment not in profile.assignment_scopes
                or card.scope not in allowed_scopes
                or card.tenant_id is not None
                or card.status
                in {
                    CardStatus.CANDIDATE,
                    CardStatus.RESEARCH_ONLY,
                    CardStatus.SUPERSEDED,
                    CardStatus.EXPIRED,
                    CardStatus.EMERGENCY_QUARANTINED,
                }
                or (card.status is CardStatus.DISPUTED and not profile.allow_disputed)
                or (card.expires_at is not None and card.expires_at <= context.as_of)
                or not card.retrieval_eligible
            ):
                scope_or_status_excluded += 1
                continue

            # 3-6. Hard explicit mismatch/effective filters; unknown is visible as a hedge penalty.
            applicable, unknown_dimensions = _applicability(card, context)
            if not applicable:
                applicability_excluded += 1
                continue

            # 7-8. Hybrid relevance + claim-scoped authority. Source class has zero authority
            # contribution outside the card's declared claim domain.
            card_text = f"{card.claim} {card.distillation_note} {card.claim_key.canonical}"
            lexical = _jaccard(objective_tokens, _tokens(card_text))
            entity = _jaccard(entity_tokens, _tokens(card_text)) if entity_tokens else None
            try:
                card_embedding = card_embeddings[card.card_version_id]
            except KeyError as exc:
                raise CardRetrievalPolicyError(
                    f"retrieval-eligible card lacks embedding: {card.card_version_id}"
                ) from exc
            semantic = _cosine(query_embedding, card_embedding)
            comparable_to_query = bool(
                _tokens(card.claim_key.canonical) & (objective_tokens | entity_tokens)
            )
            authority = (
                _SOURCE_AUTHORITY[card.source_class]
                if card.domain is domain and comparable_to_query
                else 0.0
            )
            applicability_score = max(0.0, 1.0 - 0.15 * len(unknown_dimensions))
            recency = _recency(card, context.as_of)
            confidence = _CONFIDENCE[card.confidence]
            # VT-736 — RENORMALIZE over the dimensions that actually apply. A component that is
            # INAPPLICABLE (recency on an evergreen claim: `None`) is dropped along with its weight
            # and the remainder is rescaled, rather than being folded in as a 0.0 that silently
            # docks the card. Scoring "this dimension does not exist here" the same as "this
            # dimension scores badly" is what put an entire curated corpus under the 0.62 bar.
            # When nothing is inapplicable the weights sum to 1.0 and this is arithmetically
            # identical to the previous expression — existing behaviour is untouched.
            weighted: list[tuple[float, float]] = [
                (SCORE_WEIGHTS["semantic"], semantic),
                (SCORE_WEIGHTS["lexical"], lexical),
                (SCORE_WEIGHTS["authority"], authority),
                (SCORE_WEIGHTS["applicability"], applicability_score),
                (SCORE_WEIGHTS["confidence"], confidence),
            ]
            # VT-725 extends the same rule to `entity`: a query that named no entities gives the
            # dimension nothing to match, which is inapplicability, not a zero score. NOTE this is
            # a property of the QUERY, so when it fires it drops the same weight for every card and
            # cannot re-rank a result set — it only stops the whole set being uniformly depressed.
            if entity is not None:
                weighted.append((SCORE_WEIGHTS["entity"], entity))
            if recency is not None:
                weighted.append((SCORE_WEIGHTS["recency"], recency))
            score = sum(w * v for w, v in weighted) / sum(w for w, _v in weighted)
            if score < profile.minimum_score:
                below_score_excluded += 1
                continue

            hedge_reasons = list(unknown_dimensions)
            if card.status is CardStatus.DISPUTED:
                hedge_reasons.append("disputed_claim")
            content = (
                card.claim
                if profile.depth is RetrievalDepth.CONCLUSIONS
                else f"{card.claim}\n{card.distillation_note}"
            )
            scored.append(
                RetrievedCard(
                    card=card,
                    score=score,
                    components=ScoreComponents(
                        semantic=semantic,
                        lexical=lexical,
                        entity=entity,
                        authority=authority,
                        applicability=applicability_score,
                        recency=recency,
                        confidence=confidence,
                    ),
                    content=content,
                    hedge_reasons=tuple(dict.fromkeys(hedge_reasons)),
                    estimated_tokens=_estimate_tokens(content),
                )
            )

        scored.sort(key=lambda item: (-item.score, item.card.card_version_id))

        # 9. Exact claim/value retellings from one underlying source cluster collapse to one.
        deduplicated: list[RetrievedCard] = []
        dedupe_keys: set[tuple[str, str, str]] = set()
        cluster_deduplicated = 0
        for item in scored:
            key = (
                item.card.independence_cluster,
                item.card.claim_key.canonical,
                json.dumps(item.card.claim_value.model_dump(mode="json"), sort_keys=True),
            )
            if key in dedupe_keys:
                cluster_deduplicated += 1
                continue
            dedupe_keys.add(key)
            deduplicated.append(item)

        # 10. Diversity cap, result cap, and token budget are all profile-bounded.
        selected: list[RetrievedCard] = []
        cluster_counts: dict[str, int] = defaultdict(int)
        tokens_used = 0
        diversity_or_budget_excluded = 0
        for item in deduplicated:
            cluster = item.card.independence_cluster
            if cluster_counts[cluster] >= max_per_cluster:
                diversity_or_budget_excluded += 1
                continue
            if len(selected) >= effective_top_k:
                diversity_or_budget_excluded += 1
                continue
            if tokens_used + item.estimated_tokens > effective_budget:
                diversity_or_budget_excluded += 1
                continue
            selected.append(item)
            cluster_counts[cluster] += 1
            tokens_used += item.estimated_tokens

        # 11. Comparable selected claims with different values remain explicitly visible.
        conflicts = _conflicts(selected)
        elapsed_ms = (time.perf_counter() - started) * 1_000
        # 12. No weak padding: if everything missed the minimum/hard filters, return nothing.
        return CardRetrievalResult(
            items=tuple(selected),
            conflicts=conflicts,
            no_result=not selected,
            no_result_behavior=profile.no_result_behavior.value,
            trace=CardRetrievalTrace(
                considered=len(cards),
                scope_or_status_excluded=scope_or_status_excluded,
                applicability_excluded=applicability_excluded,
                below_score_excluded=below_score_excluded,
                cluster_deduplicated=cluster_deduplicated,
                diversity_or_budget_excluded=diversity_or_budget_excluded,
                elapsed_ms=elapsed_ms,
            ),
        )


class RetrievalContextResolver(Protocol):
    def __call__(self, scope: KnowledgeScope) -> RetrievalBusinessContext: ...


class CardRegistryBrokerAdapter:
    """Unregistered adapter for the existing ``KnowledgeBroker`` contract.

    Construction does not register it anywhere. A future activation must deliberately supply this
    instance to ``KnowledgeBroker`` after the rollout flag is authorized.
    """

    def __init__(
        self,
        *,
        layer: KnowledgeLayer,
        cards: Sequence[KnowledgeCard],
        card_embeddings: Mapping[str, Sequence[float]],
        profile: RetrievalProfile,
        domain: KnowledgeDomain,
        context_resolver: RetrievalContextResolver,
        query_embedder: Callable[[list[str]], Sequence[Sequence[float]]],
        assignment_overrides: Mapping[str, CardAssignmentOverride] | None = None,
        engine: CardRetrievalEngine | None = None,
    ) -> None:
        if layer not in {KnowledgeLayer.L3, KnowledgeLayer.L4}:
            raise ValueError("card registry adapter is global L3/L4 only")
        self.layer = layer
        self._cards = tuple(cards)
        self._card_embeddings = dict(card_embeddings)
        self._profile = profile
        self._domain = domain
        self._context_resolver = context_resolver
        self._query_embedder = query_embedder
        self._assignment_overrides = dict(assignment_overrides or {})
        self._engine = engine or CardRetrievalEngine()

    def retrieve(
        self,
        scope: KnowledgeScope,
        query: KnowledgeQuery,
        *,
        limit: int,
    ) -> Sequence[EvidenceItem]:
        context = self._context_resolver(scope)
        if context.tenant_id != scope.tenant_id:
            raise CardRetrievalPolicyError("retrieval context tenant did not match runtime scope")

        from orchestrator.knowledge.embeddings import redact_for_embedding

        safe_objective = redact_for_embedding([query.objective])[0]
        vectors = self._query_embedder([safe_objective])
        if len(vectors) != 1:
            raise CardRetrievalPolicyError("query embedder must return exactly one vector")
        allowed_scope = (
            KnowledgeScopeKind.PRIOR
            if self.layer is KnowledgeLayer.L3
            else KnowledgeScopeKind.GLOBAL
        )
        result = self._engine.retrieve(
            cards=self._cards,
            card_embeddings=self._card_embeddings,
            objective=safe_objective,
            query_embedding=vectors[0],
            entity_refs=query.entity_refs,
            domain=self._domain,
            stage=query.stage.value,
            profile=self._profile,
            context=context,
            allowed_scopes=frozenset({allowed_scope}),
            assignment_overrides=self._assignment_overrides,
            top_k=min(limit, query.top_k_per_layer),
            token_budget=query.token_budget,
        )
        return tuple(self._to_evidence(item) for item in result.items)

    def _to_evidence(self, item: RetrievedCard) -> EvidenceItem:
        card = item.card
        return EvidenceItem(
            evidence_id=card.card_version_id,
            tenant_id=None,
            layer=self.layer,
            kind=MemoryKind.SEED,
            authority=card.authority,
            source_id=card.provenance.source_ids[0],
            content=item.content,
            score=item.score,
            valid_from=card.applicability.effective_from,
            valid_to=card.applicability.effective_to,
            confidence=card.confidence,
            retrieval_eligible=True,
            claim_key=card.claim_key.canonical,
            claim_value=json.dumps(card.claim_value.model_dump(mode="json"), sort_keys=True),
            metadata={
                "card_version_id": card.card_version_id,
                "corpus_version_id": card.corpus_version_id,
                "source_class": card.source_class.value,
                "independence_cluster": card.independence_cluster,
                "status": card.status.value,
                "hedge_reasons": item.hedge_reasons,
            },
        )


def voyage_query_embedder(texts: list[str]) -> Sequence[Sequence[float]]:
    """Fail-not-skip production query embedding seam; not called by any live route in VT-710."""

    from orchestrator.knowledge.embeddings import embed_redacted_texts

    return embed_redacted_texts(texts, input_type="query")


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.casefold()))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _cosine(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    if left is None or right is None:
        return 0.0
    if len(left) != len(right):
        raise CardRetrievalPolicyError("query/card embedding dimensions do not match")
    if not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


def _dimension_match(
    values: Sequence[str], context_value: str | None, label: str
) -> tuple[bool, str | None]:
    """Does this card apply on ONE dimension, and is anything about that uncertain?

    NOTE (VT-736, deliberately NOT changed here): a card declaring no values is treated as UNKNOWN
    on that dimension, not as unrestricted, so it scores below an explicitly ``universal=True`` card.
    That is a real epistemics choice — an explicit universal claim has been curated and asserted,
    while silence may just be an under-specified card — and `test_unknown_applicability_is_penalized_
    and_hedged_while_universal_matches` protects it. It is also why the whole VT-727 corpus sits at
    the applicability floor: all 64 cards declare nothing AND are not marked universal. Redefining
    silence as universal here would score 64 cards as applying to every industry, size and maturity
    on the strength of nobody having said otherwise. Whether those cards ARE universal is a claim
    about the knowledge — governance's call, not the scorer's — so it is surfaced with numbers rather
    than assumed away.
    """
    if not values:
        return True, f"unknown_{label}"
    if context_value is None:
        return True, f"unknown_business_{label}"
    normalized = context_value.casefold()
    return (any(value.casefold() == normalized for value in values), None)


def _applicability(
    card: KnowledgeCard, context: RetrievalBusinessContext
) -> tuple[bool, tuple[str, ...]]:
    applicability = card.applicability
    if applicability.effective_from and applicability.effective_from > context.as_of:
        return False, ()
    if applicability.effective_to and applicability.effective_to <= context.as_of:
        return False, ()
    if applicability.universal:
        return True, ()

    unknown: list[str] = []
    for values, context_value, label in (
        (applicability.jurisdictions, context.jurisdiction, "jurisdiction"),
        (applicability.size_bands, context.size_band, "size_band"),
        (applicability.industries, context.industry, "industry"),
        (applicability.maturity_stages, context.maturity_stage, "maturity_stage"),
        (applicability.channels, context.channel, "channel"),
    ):
        matches, reason = _dimension_match(values, context_value, label)
        if not matches:
            return False, ()
        if reason:
            unknown.append(reason)
    if applicability.effective_from is None and applicability.effective_to is None:
        unknown.append("unknown_effective_period")
    return True, tuple(unknown)


def _recency(card: KnowledgeCard, as_of: datetime) -> float | None:
    """Recency in [0,1], or **None when the card has no recency dimension at all**.

    VT-736: this used to return 0.0 for an evergreen claim (non-regulatory, no effective_to, no
    expires_at). That reads as "maximally stale" when the truth is "ageing does not apply to this
    claim" — a durable business principle is not less true this year than last. Scoring it 0.0
    silently docked the full recency weight from every evergreen card, which is most of a curated
    business corpus. Returning None lets the caller drop the weight and renormalize, so an
    inapplicable dimension neither helps nor hurts.
    """
    if (
        card.source_class not in {SourceClass.T1_REGULATORY, SourceClass.T1_VENDOR_POLICY}
        and card.applicability.effective_to is None
        and card.expires_at is None
    ):
        return None
    reference = card.applicability.effective_from or card.provenance.retrieved_at
    age_days = max(
        0.0, (as_of.astimezone(UTC) - reference.astimezone(UTC)).total_seconds() / 86_400
    )
    return max(0.0, 1.0 - age_days / (5 * 365))


def _estimate_tokens(content: str) -> int:
    return max(1, math.ceil(len(content) / _CHARS_PER_TOKEN)) + _ITEM_OVERHEAD_TOKENS


def _conflicts(items: Sequence[RetrievedCard]) -> tuple[RetrievedConflict, ...]:
    grouped: dict[str, list[KnowledgeCard]] = defaultdict(list)
    for item in items:
        grouped[item.card.claim_key.canonical].append(item.card)

    conflicts: list[RetrievedConflict] = []
    for claim_key, cards in sorted(grouped.items()):
        values = tuple(
            dict.fromkeys(
                json.dumps(card.claim_value.model_dump(mode="json"), sort_keys=True)
                for card in cards
            )
        )
        if len(values) < 2 and not any(card.status is CardStatus.DISPUTED for card in cards):
            continue
        conflicts.append(
            RetrievedConflict(
                claim_key=claim_key,
                card_version_ids=tuple(card.card_version_id for card in cards),
                claim_values=values,
            )
        )
    return tuple(conflicts)


__all__ = [
    "CardAssignmentOverride",
    "CardRegistryBrokerAdapter",
    "CardRetrievalEngine",
    "CardRetrievalPolicyError",
    "CardRetrievalResult",
    "RetrievedCard",
    "RetrievedConflict",
    "RetrievalBusinessContext",
    "voyage_query_embedder",
]
