"""VT-725 — the O8 card-retrieval CONSUMER: the seam a Manager/specialist turn calls.

``card_retrieval.py`` has held a complete policy engine since VT-710 and nothing has ever called
it: the engine operates on already-loaded ``KnowledgeCard`` values plus a mapping of embeddings,
neither of which existed anywhere at runtime.  This module is the missing half — it loads the
registry, resolves per-tenant assignment, embeds, runs the engine, and records attribution.

SHADOW ONLY.  ``CardServingResult`` carries card IDs and scores and NOTHING ELSE: there is no
field, method or property on it that returns card text, so "shadow accidentally injected" is not
a bug that can be introduced by a careless caller — the content-bearing path does not exist yet.
Building it is the ``active`` flip (D3), which is Fazal's and is gated on the treatment run
beating the sealed no-O8 baseline.

Every failure degrades to no-cards.  The Manager reasoned without cards for months; a dead Voyage
key, an empty registry, a card whose persisted row no longer satisfies the governance contract,
or a lost DB connection must all cost it exactly what it had before — never a failed turn.

Retrieval is ADVISORY TO REASONING.  A retrieved card cannot authorize a send, a spend, a consent
change or a gate bypass (ARCHITECTURE §0.1.1); the deterministic effect gates and Pillar-7 remain
the sole effect authority regardless of what any card claims.

The registry read targets migration 189's single-row projection: ``knowledge_cards`` carries
``domain``, ``source_class``, ``usage_rights``, ``independence_cluster``, ``provenance``,
``retrieval_eligible`` and its admission-corpus identity directly.  Corpus membership selects the
latest shadow snapshot so an immutable card can belong to later snapshots without inventing a new
semantic version; the serving corpus ID overrides the admission-corpus ID in attribution.  The
persisted embedding joins by immutable card-version ID and is content-digest checked before use.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from orchestrator.agent_framework.retrieval_profiles import (
    MANAGER_IDENTITY,
    retrieval_profile_for,
)
from orchestrator.knowledge.card_retrieval import (
    CardAssignmentOverride,
    CardRetrievalEngine,
    RetrievalBusinessContext,
    RetrievedCard,
)
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
    KnowledgeLayer,
    KnowledgeScopeKind,
    RetrievalStage,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
)
from orchestrator.knowledge_contracts import RetrievalProfile

logger = logging.getLogger(__name__)

#: A card's persisted row can never change (migration 182/189 rejects UPDATE on knowledge_cards),
#: so an embedding keyed by card_version_id can never go stale.  The cache is per-process and
#: bounded because VT-727 owns the persisted vector store; until then every worker embeds its own.
_EMBED_CACHE: OrderedDict[str, list[float]] = OrderedDict()
_EMBED_CACHE_MAX = 4_096
#: Chunked so ONE bad chunk costs its own cards, not the whole corpus (fail-soft per §"degrades").
_EMBED_CHUNK = 64

#: Ceiling on the candidate pool for one decision.  Each candidate becomes at least one
#: decision_evidence_links row, so an unbounded registry would otherwise turn one owner turn into
#: thousands of writes.  Raise deliberately alongside a re-measure of turn latency.
_MAX_CANDIDATE_CARDS = 500

#: India-only product; the tenant substrate has no jurisdiction field to read.  Stated as a
#: constant rather than left None because jurisdiction is the one applicability dimension where
#: "unknown" would let a foreign-jurisdiction regulatory card through on a hedge.
_JURISDICTION = "IN"

#: The GLOBAL registry's scopes.  L3 = anonymized cross-tenant priors, L4 = the curated corpus.
#: Tenant-scoped layers never reach this engine — the engine itself rejects a TENANT scope.
_LAYER_SCOPE = {
    KnowledgeLayer.L3: KnowledgeScopeKind.PRIOR,
    KnowledgeLayer.L4: KnowledgeScopeKind.GLOBAL,
}


class KnowledgeServingMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    #: Declared so the eventual flip has a name to graduate INTO.  Nothing in this module returns
    #: it and nothing consumes it; reaching it requires the content-bearing path that VT-725
    #: deliberately does not build.
    ACTIVE = "active"


def serving_mode() -> KnowledgeServingMode:
    """The env-driven mode, default OFF.  ``active`` is unreachable here by construction."""

    from orchestrator.feature_flags import knowledge_serving_mode

    if knowledge_serving_mode() == "shadow":
        return KnowledgeServingMode.SHADOW
    return KnowledgeServingMode.OFF


@dataclass(frozen=True)
class RetrievalBudget:
    """One identity's declared retrieval capability (o8 design §5.6), content-free and loggable."""

    identity: str
    domains: tuple[str, ...]
    layers: tuple[str, ...]
    top_k: int
    token_budget: int
    depth: str
    assignment_scopes: tuple[str, ...]


def declared_budget(profile: RetrievalProfile) -> RetrievalBudget:
    return RetrievalBudget(
        identity=profile.identity,
        domains=tuple(sorted(value.value for value in profile.domains)),
        layers=tuple(sorted(value.value for value in profile.layers)),
        top_k=profile.top_k,
        token_budget=profile.token_budget,
        depth=profile.depth.value,
        assignment_scopes=tuple(sorted(profile.assignment_scopes)),
    )


@dataclass(frozen=True)
class ServedCardRef:
    """One attribution row: identifiers and scores, never claim text.

    Mirrors the ``decision_evidence_links`` column set exactly so the in-memory trace and the
    persisted causality substrate cannot drift apart.
    """

    card_version_ref: str
    disposition: str
    corpus_version_ref: str | None = None
    semantic_score: float | None = None
    lexical_score: float | None = None
    entity_score: float | None = None
    combined_score: float | None = None


@dataclass(frozen=True)
class CardServingResult:
    """What a turn gets back in shadow: an attribution trace, not context.

    ``INJECTS_INTO_PROMPT`` is a ClassVar precisely so no instance can flip it — the same
    construction ``CardRetrievalResult.AUTHORIZES_EFFECTS`` uses for effect authority.
    """

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    INJECTS_INTO_PROMPT: ClassVar[bool] = False

    mode: KnowledgeServingMode
    identity: str
    budget: RetrievalBudget | None
    #: Every corpus version this decision drew from — the handle §6 ablation and rollback need.
    corpus_version_refs: tuple[str, ...]
    candidates: int
    refs: tuple[ServedCardRef, ...]
    conflicts: int
    no_result: bool
    #: Non-None whenever the turn got fewer cards than the registry could have given it. Named so
    #: a shadow read can separate "the corpus had nothing applicable" from "retrieval broke".
    degraded_reason: str | None
    evidence_links_written: int
    evidence_links_error: str | None
    elapsed_ms: float

    @property
    def selected_card_refs(self) -> tuple[str, ...]:
        return tuple(ref.card_version_ref for ref in self.refs if ref.disposition == "selected")


@dataclass(frozen=True)
class LoadedCorpus:
    """The candidate pool for one identity+tenant, plus what the load had to drop and why."""

    cards: tuple[KnowledgeCard, ...]
    overrides: Mapping[str, CardAssignmentOverride]
    persisted_embeddings: Mapping[str, list[float]]
    unmappable_rows: int
    #: The pool hit _MAX_CANDIDATE_CARDS, so eligible cards were left out of this decision.
    #: Surfaced rather than swallowed: a silently truncated pool would look like a corpus that
    #: simply had nothing to say, which is the one reading of "no cards" that must stay wrong.
    truncated: bool = False


def retrieve_cards_for_turn(
    *,
    tenant_id: UUID | str,
    run_id: UUID | str,
    decision_id: str,
    objective: str,
    stage: RetrievalStage,
    domain: KnowledgeDomain,
    identity: str = MANAGER_IDENTITY,
    entity_refs: Sequence[str] = (),
    context: RetrievalBusinessContext | None = None,
    conn: Any = None,
) -> CardServingResult:
    """Retrieve, score and record knowledge cards for one decision.  Shadow-only; fail-soft.

    ``decision_id`` is the attribution key: it must identify the decision within the run (the
    ``(tenant, run, decision, card, disposition)`` uniqueness in migration 183 is what makes a
    replayed step idempotent rather than a double-count in the ablation data).
    """

    started = time.perf_counter()
    mode = serving_mode()
    if mode is not KnowledgeServingMode.SHADOW:
        return _degraded(mode, identity, None, "serving_off", started)

    budget: RetrievalBudget | None = None
    try:
        profile = retrieval_profile_for(identity)
        budget = declared_budget(profile)
        resolved_context = context or resolve_business_context(tenant_id)
        if resolved_context.tenant_id != _as_uuid(tenant_id):
            raise ValueError("business context tenant did not match the retrieval tenant")

        corpus = load_serving_corpus(tenant_id, profile, conn=conn)
        if not corpus.cards:
            # An empty pool because the registry has nothing eligible and an empty pool because
            # every row failed the contract are different problems; say which.
            reason = "empty_candidate_pool"
            if corpus.unmappable_rows:
                reason += f",rows_unmappable:{corpus.unmappable_rows}"
            return _degraded(mode, identity, budget, reason, started)

        query_embedding = embed_query(objective)
        embeddings, unembeddable = embed_cards(
            corpus.cards, persisted=corpus.persisted_embeddings
        )
        embeddable = tuple(
            card
            for card in corpus.cards
            if card.card_version_id in embeddings
            and len(embeddings[card.card_version_id]) == len(query_embedding)
        )
        if not embeddable:
            return _degraded(mode, identity, budget, "no_embeddable_cards", started)

        result = CardRetrievalEngine().retrieve(
            cards=embeddable,
            card_embeddings=embeddings,
            objective=objective,
            query_embedding=query_embedding,
            entity_refs=tuple(entity_refs),
            domain=domain,
            stage=stage.value,
            profile=profile,
            context=resolved_context,
            allowed_scopes=_allowed_scopes(profile),
            assignment_overrides=corpus.overrides,
        )
    except Exception as exc:  # noqa: BLE001 — a knowledge miss is never a failed turn
        # Type only, no exc_info: a pydantic ValidationError renders the offending card's claim
        # text into its message, and card content has no business in a log line.
        logger.warning(
            "card_serving: retrieval degraded to no-cards (tenant=%s identity=%s decision=%s "
            "error=%s)",
            tenant_id, identity, decision_id, type(exc).__name__,
        )
        return _degraded(mode, identity, budget, f"error:{type(exc).__name__}", started)

    refs = _attribution_refs(candidates=embeddable, selected=result.items)
    written, links_error = record_evidence_links(
        tenant_id,
        run_id=run_id,
        decision_id=decision_id,
        stage=stage,
        refs=refs,
        conn=conn,
    )
    notes: list[str] = []
    if unembeddable:
        notes.append(f"cards_unembeddable:{len(unembeddable)}")
    if corpus.unmappable_rows:
        notes.append(f"rows_unmappable:{corpus.unmappable_rows}")
    if corpus.truncated:
        notes.append("candidate_pool_truncated")
    return CardServingResult(
        mode=mode,
        identity=identity,
        budget=budget,
        corpus_version_refs=tuple(
            sorted({ref.corpus_version_ref for ref in refs if ref.corpus_version_ref})
        ),
        candidates=len(embeddable),
        refs=refs,
        conflicts=len(result.conflicts),
        no_result=result.no_result,
        degraded_reason=",".join(notes) or None,
        evidence_links_written=written,
        evidence_links_error=links_error,
        elapsed_ms=(time.perf_counter() - started) * 1_000,
    )


def _degraded(
    mode: KnowledgeServingMode,
    identity: str,
    budget: RetrievalBudget | None,
    reason: str,
    started: float,
) -> CardServingResult:
    return CardServingResult(
        mode=mode,
        identity=identity,
        budget=budget,
        corpus_version_refs=(),
        candidates=0,
        refs=(),
        conflicts=0,
        no_result=True,
        degraded_reason=reason,
        evidence_links_written=0,
        evidence_links_error=None,
        elapsed_ms=(time.perf_counter() - started) * 1_000,
    )


def _allowed_scopes(profile: RetrievalProfile) -> frozenset[KnowledgeScopeKind]:
    scopes = frozenset(
        _LAYER_SCOPE[layer] for layer in profile.layers if layer in _LAYER_SCOPE
    )
    if not scopes:
        raise ValueError(f"profile {profile.identity!r} declares no global knowledge layer")
    return scopes


def _attribution_refs(
    *, candidates: Sequence[KnowledgeCard], selected: Sequence[RetrievedCard]
) -> tuple[ServedCardRef, ...]:
    """Three dispositions per decision: every candidate is ``retrieved``; the engine's output is
    ``selected`` with its component scores; everything else is ``rejected``.

    Rejected rows carry no scores on purpose.  The engine's trace reports exclusion COUNTS, not
    per-card outcomes, so a score here would be invented — and invented numbers in the causality
    substrate are worse than missing ones when §6 comes to attribute a regression.
    """

    chosen = {item.card.card_version_id: item for item in selected}
    refs: list[ServedCardRef] = []
    for card in candidates:
        corpus_ref = card.corpus_version_id
        refs.append(
            ServedCardRef(
                card_version_ref=card.card_version_id,
                disposition="retrieved",
                corpus_version_ref=corpus_ref,
            )
        )
        item = chosen.get(card.card_version_id)
        if item is None:
            refs.append(
                ServedCardRef(
                    card_version_ref=card.card_version_id,
                    disposition="rejected",
                    corpus_version_ref=corpus_ref,
                )
            )
            continue
        refs.append(
            ServedCardRef(
                card_version_ref=card.card_version_id,
                disposition="selected",
                corpus_version_ref=corpus_ref,
                semantic_score=_bounded(item.components.semantic),
                lexical_score=_bounded(item.components.lexical),
                entity_score=_bounded(item.components.entity),
                combined_score=_bounded(item.score),
            )
        )
    return tuple(refs)


def _bounded(value: float | None) -> float | None:
    """Clamp to the [0,1] CHECK constraints so a float edge can never fail the whole write.

    VT-725: ``None`` means the dimension did not APPLY to this turn (entity scoring when the query
    named no entities). It is persisted as SQL NULL — the column is already nullable — rather than
    coerced to 0.0, because "did not apply" and "scored zero" are exactly the distinction the
    renormalization exists to preserve, and the shadow ledger is what anyone auditing the floor
    reads back.
    """

    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


# --------------------------------------------------------------------------------------------
# Registry load
# --------------------------------------------------------------------------------------------

#: Migration 189's projection: one card is one row.  ``retrieval_eligible`` is the CURATOR's
#: admission decision and is therefore a predicate, not something this layer re-derives — a card
#: the pipeline marked ineligible must not serve because a consumer thought its status looked fine.
_CARD_SQL = """
    WITH serving_corpus AS (
        SELECT id
        FROM knowledge_corpus_versions
        WHERE status = 'shadow' AND admission_verdict = 'pending'
        ORDER BY version DESC
        LIMIT 1
    )
    SELECT
        c.id, c.card_key, c.version, c.claim, c.claim_key, c.claim_value, c.distillation_note,
        c.jurisdictions, c.size_bands, c.industries, c.maturity_stages, c.channels,
        c.applicability_universal, c.effective_from, c.effective_until,
        c.authority, c.confidence, c.scope, c.status, c.retention_class, c.expires_at,
        c.default_assignment, c.domain, c.source_class, c.usage_rights,
        c.independence_cluster, c.corroboration_cluster_count, c.provenance,
        c.retrieval_eligible, serving_corpus.id AS corpus_version_id,
        e.embedding::text AS persisted_embedding, e.embedding_model, e.content_digest
    FROM knowledge_cards c
    JOIN knowledge_corpus_members member ON member.card_id = c.id
    JOIN serving_corpus ON serving_corpus.id = member.corpus_version_id
    LEFT JOIN knowledge_card_embeddings e ON e.card_id = c.id
    WHERE c.retrieval_eligible
      AND c.status IN ('validated', 'disputed')
      AND c.domain = ANY(%s::text[])
      AND c.scope = ANY(%s::text[])
      AND (c.expires_at IS NULL OR c.expires_at > now())
    ORDER BY c.created_at DESC, c.id
    LIMIT %s
"""

_ASSIGNMENT_SQL = """
    SELECT card_id, scope, enabled FROM knowledge_card_assignments
    WHERE tenant_id = %s
"""


def load_serving_corpus(
    tenant_id: UUID | str, profile: RetrievalProfile, *, conn: Any = None
) -> LoadedCorpus:
    """Load this identity's candidate pool and the tenant's assignment overrides.

    The SQL applies the DECLARED BUDGET (corpus domains and global layers) and nothing else about
    assignment: resolving ``default_assignment`` against the tenant override stays in the engine
    so there is exactly ONE implementation of the flip mechanism Fazal ratified.
    """

    if conn is not None:
        return _load_serving_corpus(conn, tenant_id, profile)
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as tenant_conn:
        return _load_serving_corpus(tenant_conn, tenant_id, profile)


def _load_serving_corpus(
    conn: Any, tenant_id: UUID | str, profile: RetrievalProfile
) -> LoadedCorpus:
    rows = conn.execute(
        _CARD_SQL,
        (
            sorted(value.value for value in profile.domains),
            sorted(value.value for value in _allowed_scopes(profile)),
            _MAX_CANDIDATE_CARDS,
        ),
    ).fetchall()
    cards: list[KnowledgeCard] = []
    persisted_embeddings: dict[str, list[float]] = {}
    unmappable = 0
    for row in rows:
        card = _card_from_row(row)
        if card is None:
            unmappable += 1
            continue
        cards.append(card)
        persisted = _persisted_embedding(row, card)
        if persisted is not None:
            persisted_embeddings[card.card_version_id] = persisted

    tenant_uuid = _as_uuid(tenant_id)
    overrides: dict[str, CardAssignmentOverride] = {}
    for row in conn.execute(_ASSIGNMENT_SQL, (str(tenant_id),)).fetchall():
        try:
            overrides[str(_column(row, 0, "card_id"))] = CardAssignmentOverride(
                card_version_id=str(_column(row, 0, "card_id")),
                tenant_id=tenant_uuid,
                scope=str(_column(row, 1, "scope")),
                enabled=bool(_column(row, 2, "enabled")),
            )
        except ValueError:
            # A malformed persisted override must not silently WIDEN access: dropping it falls
            # back to the card's global default_assignment, which is the narrower state.
            logger.warning("card_serving: dropped malformed assignment override")
    return LoadedCorpus(
        cards=tuple(cards),
        overrides=overrides,
        persisted_embeddings=persisted_embeddings,
        unmappable_rows=unmappable,
        truncated=len(rows) >= _MAX_CANDIDATE_CARDS,
    )


def _card_from_row(row: Any) -> KnowledgeCard | None:
    """Map one registry row to the governed contract, or None when the row cannot satisfy it.

    A persisted row that no longer validates (a T4 card past its mandatory expiry, a claim_key
    that lost a dimension, provenance that lost its publisher) is DROPPED, not repaired and not
    raised: the governance invariants in ``KnowledgeCard`` are the admission gate, and a row that
    fails them has no business reaching reasoning.
    """

    try:
        provenance = _column(row, 27, "provenance")
        corpus_version_id = _column(row, 29, "corpus_version_id")
        return KnowledgeCard(
            card_id=str(_column(row, 1, "card_key")),
            card_version_id=str(_column(row, 0, "id")),
            card_version=int(_column(row, 2, "version")),
            corpus_version_id=str(corpus_version_id) if corpus_version_id else None,
            claim=str(_column(row, 3, "claim")),
            distillation_note=str(_column(row, 6, "distillation_note")),
            claim_key=_claim_key(str(_column(row, 4, "claim_key"))),
            claim_value=_claim_value(_column(row, 5, "claim_value")),
            source_class=SourceClass(str(_column(row, 23, "source_class"))),
            domain=KnowledgeDomain(str(_column(row, 22, "domain"))),
            authority=EvidenceAuthority(str(_column(row, 15, "authority"))),
            confidence=EvidenceConfidence(str(_column(row, 16, "confidence"))),
            independence_cluster=str(_column(row, 25, "independence_cluster")),
            corroboration_cluster_count=max(
                1, int(_column(row, 26, "corroboration_cluster_count") or 1)
            ),
            applicability=Applicability(
                jurisdictions=tuple(_column(row, 7, "jurisdictions") or ()),
                size_bands=tuple(_column(row, 8, "size_bands") or ()),
                industries=tuple(_column(row, 9, "industries") or ()),
                maturity_stages=tuple(_column(row, 10, "maturity_stages") or ()),
                channels=tuple(_column(row, 11, "channels") or ()),
                effective_from=_aware(_column(row, 13, "effective_from")),
                effective_to=_aware(_column(row, 14, "effective_until")),
                universal=bool(_column(row, 12, "applicability_universal")),
            ),
            provenance=CardProvenance(
                source_ids=tuple(str(value) for value in provenance["source_ids"]),
                publisher=str(provenance["publisher"]),
                retrieved_at=_datetime(provenance["retrieved_at"]),
                tainted=bool(provenance.get("tainted", True)),
            ),
            usage_rights=_usage_rights(_column(row, 24, "usage_rights")),
            retention_class=str(_column(row, 19, "retention_class")),
            scope=KnowledgeScopeKind(str(_column(row, 17, "scope"))),
            default_assignment=str(_column(row, 21, "default_assignment")),
            status=CardStatus(str(_column(row, 18, "status"))),
            retrieval_eligible=bool(_column(row, 28, "retrieval_eligible")),
            expires_at=_aware(_column(row, 20, "expires_at")),
        )
    except Exception as exc:  # noqa: BLE001 — one ungovernable row must not cost the whole corpus
        # The card id identifies the row for curation; the exception TYPE is all that is safe to
        # print, because a pydantic ValidationError message embeds the claim it rejected.
        logger.warning(
            "card_serving: dropped unmappable registry row (card=%s error=%s)",
            _column(row, 0, "id"), type(exc).__name__,
        )
        return None


def _claim_key(canonical: str) -> ClaimKey:
    parts = canonical.split("|")
    if len(parts) != 5:
        raise ValueError(f"claim_key is not a five-dimension canonical value: {len(parts)} parts")
    subject, predicate, jurisdiction, population, channel = parts
    return ClaimKey(
        subject=subject,
        predicate=predicate,
        jurisdiction=jurisdiction,
        population=population,
        channel=channel,
    )


def _claim_value(payload: Any) -> TypedClaimValue:
    """Rebuild the strictly-typed claim value from JSONB, where every scalar arrived as text."""

    value_type = ClaimValueType(str(payload["value_type"]))
    raw = payload.get("value")
    if value_type is ClaimValueType.TEXT:
        value: Any = str(raw)
    elif value_type is ClaimValueType.BOOLEAN:
        value = bool(raw)
    elif value_type is ClaimValueType.INTEGER:
        value = int(raw)
    elif value_type is ClaimValueType.DECIMAL:
        value = Decimal(str(raw))
    elif value_type is ClaimValueType.DATE:
        value = raw if _is_plain_date(raw) else date.fromisoformat(str(raw))
    else:
        value = _datetime(raw)
    unit = payload.get("unit")
    return TypedClaimValue(
        value_type=value_type, value=value, unit=str(unit) if unit is not None else None
    )


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _usage_rights(payload: Any) -> UsageRights:
    """Prefer the persisted rights record; fall back to an explicit ``unknown`` when it is absent
    or malformed.  Rights metadata cannot gate serving (CL-2026-07-29b), so a shape mismatch must
    cost the card its authority provenance — not its place in the corpus."""

    if isinstance(payload, Mapping):
        try:
            return UsageRights.model_validate(dict(payload))
        except Exception as exc:  # noqa: BLE001 — fall through to the honest unknown record
            logger.debug("card_serving: usage_rights did not validate (%s)", type(exc).__name__)
    return UsageRights(
        status=UsageRightsStatus.UNKNOWN,
        reviewed_at=datetime.now(UTC),
        reviewed_by="source-record:unavailable",
    )


def _datetime(value: Any) -> datetime:
    """Parse a JSONB timestamp; the driver returns real datetimes only for typed columns."""

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    return value if value.utcoffset() is not None else value.replace(tzinfo=UTC)


def _column(row: Any, index: int, name: str) -> Any:
    """Read psycopg tuple rows and dict_row rows without coupling this seam to pool config."""

    return row[name] if isinstance(row, dict) else row[index]


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


# --------------------------------------------------------------------------------------------
# Business context
# --------------------------------------------------------------------------------------------


def resolve_business_context(tenant_id: UUID | str) -> RetrievalBusinessContext:
    """Assemble the applicability context from the tenant's existing L1 profile.

    Unknown dimensions are left None deliberately: the engine treats an unknown dimension as a
    HEDGE (``unknown_business_*``, a small applicability penalty) rather than an exclusion, so a
    thin profile costs a tenant ranking precision, never access to its own applicable knowledge.
    Any read failure yields the same thin context — the tenant still retrieves.
    """

    profile: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    try:
        from orchestrator.knowledge.business_context import read_business_context

        ctx = read_business_context(tenant_id)
        profile = ctx.profile or {}
        identity = ctx.identity or {}
    except Exception:  # noqa: BLE001 — advisory context, never a gate
        logger.warning("card_serving: business context read failed (thin context)", exc_info=True)

    industry = profile.get("archetype") or identity.get("business_type")
    return RetrievalBusinessContext(
        tenant_id=_as_uuid(tenant_id),
        jurisdiction=_JURISDICTION,
        size_band=_text(profile.get("size_band")),
        industry=_text(industry),
        maturity_stage=_text(identity.get("phase")),
        # WhatsApp is the conversational primary (CL-443); a card scoped to another channel is
        # genuinely not applicable to how this product talks to an owner.
        channel="whatsapp",
        as_of=datetime.now(UTC),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered[:100] or None


# --------------------------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------------------------


def embed_query(objective: str) -> list[float]:
    """Embed the turn's objective.  Raises — the caller degrades the whole retrieval to no-cards,
    because a hybrid retrieval without a query vector is not a degraded result, it is a wrong one."""

    from orchestrator.knowledge.embeddings import embed_redacted_texts

    vectors = embed_redacted_texts([objective], input_type="query")
    if len(vectors) != 1 or not vectors[0]:
        raise ValueError("query embedder did not return exactly one non-empty vector")
    return list(vectors[0])


def embed_cards(
    cards: Sequence[KnowledgeCard],
    *,
    persisted: Mapping[str, Sequence[float]] | None = None,
) -> tuple[dict[str, list[float]], tuple[str, ...]]:
    """Embed every card not already cached; return the vectors plus the ids that could not embed.

    An unembeddable card is EXCLUDED from retrieval.  It is never a failed turn and never a
    zero-vector stand-in: a zero vector scores 0.0 semantic and would silently rank the card as
    merely weak rather than absent, which is the kind of quiet wrongness §6 cannot attribute.
    """

    from orchestrator.knowledge.embeddings import embed_redacted_texts

    vectors: dict[str, list[float]] = {}
    pending: list[KnowledgeCard] = []
    persisted = persisted or {}
    for card in cards:
        stored = persisted.get(card.card_version_id)
        if stored is not None:
            values = [float(value) for value in stored]
            if values:
                vectors[card.card_version_id] = values
                _cache_embedding(card.card_version_id, values)
                continue
        cached = _EMBED_CACHE.get(card.card_version_id)
        if cached is None:
            pending.append(card)
            continue
        _EMBED_CACHE.move_to_end(card.card_version_id)
        vectors[card.card_version_id] = cached

    failed: list[str] = []
    for start in range(0, len(pending), _EMBED_CHUNK):
        chunk = pending[start : start + _EMBED_CHUNK]
        try:
            embedded = embed_redacted_texts(
                [f"{card.claim}\n{card.distillation_note}" for card in chunk],
                input_type="document",
            )
            if len(embedded) != len(chunk):
                raise ValueError("embedder returned a vector count that does not match the batch")
        except Exception as exc:  # noqa: BLE001 — this chunk's cards drop out, turn continues
            # No exc_info: a provider error can echo the submitted text back in its message.
            logger.warning(
                "card_serving: card embedding chunk failed (%s); %d cards excluded",
                type(exc).__name__, len(chunk),
            )
            failed.extend(card.card_version_id for card in chunk)
            continue
        for card, vector in zip(chunk, embedded, strict=True):
            if not vector:
                failed.append(card.card_version_id)
                continue
            values = list(vector)
            vectors[card.card_version_id] = values
            _cache_embedding(card.card_version_id, values)
    return vectors, tuple(failed)


def _persisted_embedding(row: Any, card: KnowledgeCard) -> list[float] | None:
    """Accept a stored vector only when model, dimensions, and immutable content all match."""

    from orchestrator.knowledge.embeddings import EMBED_DIM, EMBED_MODEL
    from orchestrator.knowledge.persisted_embeddings import card_content_digest

    if isinstance(row, Mapping):
        raw = row.get("persisted_embedding")
        model = row.get("embedding_model")
        digest = row.get("content_digest")
    else:
        raw, model, digest = row[30], row[31], row[32]
    if raw is None:
        return None
    if model != EMBED_MODEL or digest != card_content_digest(card):
        return None
    if isinstance(raw, str):
        values = [float(value) for value in raw.strip("[]").split(",") if value]
    else:
        values = [float(value) for value in raw]
    return values if len(values) == EMBED_DIM else None


def _cache_embedding(card_version_id: str, vector: list[float]) -> None:
    _EMBED_CACHE[card_version_id] = vector
    _EMBED_CACHE.move_to_end(card_version_id)
    while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)


# --------------------------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------------------------

_EVIDENCE_LINK_SQL = """
    INSERT INTO decision_evidence_links (
        tenant_id, run_id, decision_id, corpus_version_id, corpus_version_ref,
        card_id, card_version_ref, retrieval_stage, disposition,
        semantic_score, lexical_score, entity_score, combined_score
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (tenant_id, run_id, decision_id, card_version_ref, disposition) DO NOTHING
"""


def record_evidence_links(
    tenant_id: UUID | str,
    *,
    run_id: UUID | str,
    decision_id: str,
    stage: RetrievalStage,
    refs: Sequence[ServedCardRef],
    conn: Any = None,
) -> tuple[int, str | None]:
    """Persist the causality substrate: ids and scores, PII-free by construction.

    Returns ``(rows_written, error)``.  A write failure is reported, never raised — attribution is
    what makes a harmful card traceable later, but losing it must not cost the owner this turn.
    """

    if not refs:
        return 0, None
    # corpus_version_ref is NOT NULL (migration 183). A card outside any corpus version has
    # nothing honest to attribute to, so its rows are skipped and COUNTED rather than dropped.
    attributable = [ref for ref in refs if ref.corpus_version_ref]
    skipped = len(refs) - len(attributable)
    error = f"cards_without_corpus_version:{skipped}" if skipped else None
    if not attributable:
        return 0, error or "no_corpus_version"
    try:
        rows = [
            (
                str(tenant_id), str(run_id), decision_id,
                ref.corpus_version_ref, ref.corpus_version_ref,
                ref.card_version_ref, ref.card_version_ref,
                stage.value, ref.disposition,
                ref.semantic_score, ref.lexical_score, ref.entity_score, ref.combined_score,
            )
            for ref in attributable
        ]
        if conn is not None:
            return _write_evidence_links(conn, rows), error
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as tenant_conn:
            return _write_evidence_links(tenant_conn, rows), error
    except Exception as exc:  # noqa: BLE001 — advisory attribution, never a gate
        logger.warning(
            "card_serving: evidence-link write failed (tenant=%s decision=%s)",
            tenant_id, decision_id, exc_info=True,
        )
        return 0, f"error:{type(exc).__name__}"


def _write_evidence_links(conn: Any, rows: Sequence[tuple[Any, ...]]) -> int:
    with conn.cursor() as cur:
        cur.executemany(_EVIDENCE_LINK_SQL, rows)
        written = cur.rowcount
    return written if written and written > 0 else 0


__all__ = [
    "CardServingResult",
    "KnowledgeServingMode",
    "LoadedCorpus",
    "RetrievalBudget",
    "ServedCardRef",
    "declared_budget",
    "embed_cards",
    "embed_query",
    "load_serving_corpus",
    "record_evidence_links",
    "resolve_business_context",
    "retrieve_cards_for_turn",
    "serving_mode",
]
