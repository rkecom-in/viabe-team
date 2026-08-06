"""VT-710 tests for the scoped 12-step card retrieval policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.agent_framework.retrieval_profiles import (  # noqa: E402
    MANAGER_RETRIEVAL_PROFILE,
    specialist_retrieval_profile,
)
from orchestrator.knowledge.card_retrieval import (  # noqa: E402
    CardAssignmentOverride,
    CardRegistryBrokerAdapter,
    CardRetrievalEngine,
    CardRetrievalPolicyError,
    CardRetrievalResult,
    RetrievalBusinessContext,
)
from orchestrator.knowledge.contracts import (  # noqa: E402
    Applicability,
    CardProvenance,
    CardStatus,
    ClaimKey,
    EvidenceAuthority,
    EvidenceConfidence,
    KnowledgeCard,
    KnowledgeBundle,
    KnowledgeDomain,
    KnowledgeLayer,
    KnowledgeQuery,
    KnowledgeScope,
    KnowledgeScopeKind,
    RetrievalStage,
    SourceClass,
    TypedClaimValue,
    UsageRights,
    UsageRightsStatus,
)
from orchestrator.knowledge_contracts import (  # noqa: E402
    KNOWLEDGE_RETRIEVAL_AUTHORIZES_EFFECTS,
    KnowledgeAssignmentScope,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
TENANT = UUID("11111111-1111-4111-8111-111111111111")
PROFILE = specialist_retrieval_profile(
    identity="sales_recovery_agent",
    domains=frozenset({KnowledgeDomain.SALES}),
    top_k=8,
    token_budget=3_000,
)


def context(**updates: object) -> RetrievalBusinessContext:
    values = {
        "tenant_id": TENANT,
        "jurisdiction": "IN",
        "size_band": "small",
        "industry": "retail",
        "maturity_stage": "growth",
        "channel": "whatsapp",
        "as_of": NOW,
    }
    values.update(updates)
    return RetrievalBusinessContext.model_validate(values)


def card(
    name: str,
    *,
    status: CardStatus = CardStatus.VALIDATED,
    source_class: SourceClass = SourceClass.T2_EVIDENCE,
    domain: KnowledgeDomain = KnowledgeDomain.SALES,
    scope: KnowledgeScopeKind = KnowledgeScopeKind.GLOBAL,
    applicability: Applicability | None = None,
    cluster: str | None = None,
    claim_value: str = "use bounded follow-up",
    retrieval_eligible: bool = True,
    expires_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    default_assignment: str = "specialist:sales_recovery_agent",
    usage_rights: UsageRights | None = None,
) -> KnowledgeCard:
    return KnowledgeCard(
        card_id=f"card:{name}",
        card_version_id=f"card:{name}:v1",
        card_version=1,
        claim=f"Bounded follow-up improves response discipline for {name}.",
        distillation_note=f"Use evidence and stop conditions for {name}; never promise outcomes.",
        claim_key=ClaimKey(
            subject="sales follow up",
            predicate="improves response discipline",
            jurisdiction="in",
            population="small",
            channel="whatsapp",
        ),
        claim_value=TypedClaimValue(value_type="text", value=claim_value),
        source_class=source_class,
        domain=domain,
        authority=EvidenceAuthority.SEED,
        confidence=EvidenceConfidence.HIGH,
        independence_cluster=cluster or f"cluster:{name}",
        corroboration_cluster_count=2,
        applicability=applicability
        or Applicability(
            jurisdictions=("IN",),
            size_bands=("small",),
            industries=("retail",),
            maturity_stages=("growth",),
            channels=("whatsapp",),
            effective_from=NOW - timedelta(days=30),
        ),
        provenance=CardProvenance(
            source_ids=(f"source:{name}",),
            publisher="test publisher",
            retrieved_at=retrieved_at or NOW - timedelta(days=30),
            tainted=True,
        ),
        usage_rights=usage_rights
        or UsageRights(
            status=UsageRightsStatus.PERMISSION_GRANTED,
            allows_extraction=True,
            allows_embedding=True,
            allows_retrieval=True,
            reviewed_at=NOW,
            reviewed_by="test",
        ),
        retention_class="lifecycle_managed",
        scope=scope,
        status=status,
        retrieval_eligible=retrieval_eligible,
        expires_at=expires_at,
        default_assignment=default_assignment,
    )


def retrieve(cards, **updates):  # noqa: ANN001, ANN202
    values = {
        "cards": cards,
        "card_embeddings": {value.card_version_id: [1.0, 0.0] for value in cards},
        "objective": "bounded sales follow up improves response discipline",
        "query_embedding": [1.0, 0.0],
        "entity_refs": ("whatsapp", "retail"),
        "domain": KnowledgeDomain.SALES,
        "stage": RetrievalStage.SPECIALIST.value,
        "profile": PROFILE,
        "context": context(),
        "allowed_scopes": frozenset({KnowledgeScopeKind.GLOBAL}),
    }
    values.update(updates)
    return CardRetrievalEngine().retrieve(**values)


def test_unknown_source_rights_do_not_exclude_an_admitted_original_claim() -> None:
    unknown = card(
        "unknown-rights",
        usage_rights=UsageRights(
            status=UsageRightsStatus.UNKNOWN,
            reviewed_at=NOW,
            reviewed_by="source-record:test",
        ),
    )
    result = retrieve([unknown])
    assert [item.card.card_version_id for item in result.items] == [unknown.card_version_id]


def test_hard_applicability_status_scope_and_expiry_filters_run_before_ranking() -> None:
    valid = card("valid")
    mismatch = card(
        "mismatch",
        applicability=Applicability(
            jurisdictions=("US",),
            size_bands=("small",),
            channels=("whatsapp",),
            effective_from=NOW - timedelta(days=1),
        ),
    )
    future = card(
        "future",
        applicability=Applicability(
            jurisdictions=("IN",), effective_from=NOW + timedelta(seconds=1)
        ),
    )
    result = retrieve(
        [
            valid,
            mismatch,
            future,
            card("candidate", status=CardStatus.CANDIDATE, retrieval_eligible=False),
            card("quarantine", status=CardStatus.EMERGENCY_QUARANTINED, retrieval_eligible=False),
            card("expired", expires_at=NOW),
            card("prior", scope=KnowledgeScopeKind.PRIOR),
        ]
    )
    assert [item.card.card_id for item in result.items] == [valid.card_id]
    assert result.trace.scope_or_status_excluded == 4
    assert result.trace.applicability_excluded == 2

    with pytest.raises(CardRetrievalPolicyError, match="cannot include tenant scope"):
        retrieve([valid], allowed_scopes=frozenset({KnowledgeScopeKind.TENANT}))


def test_unknown_applicability_is_penalized_and_hedged_while_universal_matches() -> None:
    unknown = card("unknown", applicability=Applicability())
    universal = card("universal", applicability=Applicability(universal=True))
    result = retrieve([unknown, universal], profile=PROFILE)
    by_name = {item.card.card_id: item for item in result.items}
    assert by_name[universal.card_id].components.applicability == 1.0
    assert by_name[unknown.card_id].components.applicability < 1.0
    assert "unknown_jurisdiction" in by_name[unknown.card_id].hedge_reasons
    assert by_name[universal.card_id].score > by_name[unknown.card_id].score


def test_claim_scoped_authority_does_not_let_off_domain_t1_beat_matching_t2() -> None:
    matching = card("matching-t2", source_class=SourceClass.T2_EVIDENCE)
    off_domain = card(
        "off-domain-t1",
        source_class=SourceClass.T1_REGULATORY,
        domain=KnowledgeDomain.COMPLIANCE,
    )
    result = retrieve([matching, off_domain])
    assert result.items[0].card.card_id == matching.card_id
    assert result.items[0].components.authority == 0.8
    off = next(item for item in result.items if item.card.card_id == off_domain.card_id)
    assert off.components.authority == 0.0


def test_newer_generic_practitioner_does_not_beat_stronger_older_evidence_by_recency() -> None:
    evidence = card(
        "older-evidence",
        source_class=SourceClass.T2_EVIDENCE,
        retrieved_at=NOW - timedelta(days=1_000),
    )
    newer = card(
        "newer-practitioner",
        source_class=SourceClass.T3_PRACTITIONER,
        retrieved_at=NOW,
    )
    result = retrieve([newer, evidence])
    assert result.items[0].card.card_id == evidence.card_id
    # VT-736: both cards are evergreen (non-regulatory, no effective_to/expires_at), so recency does
    # not APPLY to either — now None rather than 0.0, and its weight is renormalized away instead of
    # being folded in as a zero. The invariant this test actually guards is the line above: recency
    # must not let a newer generic practitioner card outrank stronger older evidence. It still holds.
    assert all(item.components.recency is None for item in result.items)


def test_cluster_dedup_diversity_budget_and_conflicts_are_explicit() -> None:
    first = card("first", cluster="same-study", claim_value="yes")
    retelling = card("retelling", cluster="same-study", claim_value="yes")
    conflict = card(
        "conflict",
        status=CardStatus.DISPUTED,
        cluster="independent-study",
        claim_value="no",
    )
    result = retrieve([first, retelling, conflict], top_k=3, max_per_cluster=1)
    assert len(result.items) == 2
    assert result.trace.cluster_deduplicated == 1
    assert len(result.conflicts) == 1
    assert set(result.conflicts[0].claim_values) == {
        '{"unit": null, "value": "yes", "value_type": "text"}',
        '{"unit": null, "value": "no", "value_type": "text"}',
    }
    disputed = next(item for item in result.items if item.card.status is CardStatus.DISPUTED)
    assert "disputed_claim" in disputed.hedge_reasons


def test_minimum_score_returns_nothing_and_declared_behavior() -> None:
    weak_profile = specialist_retrieval_profile(
        identity="sales_recovery_agent",
        domains=frozenset({KnowledgeDomain.SALES}),
        top_k=4,
        token_budget=1_000,
    )
    # Orthogonal embeddings + unrelated words cannot clear the default minimum score.
    result = retrieve(
        [card("weak")],
        profile=weak_profile,
        objective="unrelated inventory tax",
        query_embedding=[0.0, 1.0],
        entity_refs=(),
    )
    assert result.items == ()
    assert result.no_result is True
    assert result.no_result_behavior == "hedge"
    assert result.trace.below_score_excluded == 1

    with pytest.raises(CardRetrievalPolicyError, match="dimensions do not match"):
        retrieve([card("bad-vector")], query_embedding=[1.0, 0.0, 0.0])
    with pytest.raises(CardRetrievalPolicyError, match="lacks embedding"):
        retrieve([card("missing-vector")], card_embeddings={})


def test_manager_is_broad_knowledge_holder_but_receives_conclusions_only() -> None:
    management = card(
        "management",
        domain=KnowledgeDomain.MANAGEMENT,
        default_assignment=KnowledgeAssignmentScope.MANAGER_GLOBAL.value,
    )
    result = retrieve(
        [management],
        domain=KnowledgeDomain.MANAGEMENT,
        stage=RetrievalStage.REVIEW.value,
        profile=MANAGER_RETRIEVAL_PROFILE,
    )
    assert result.items[0].content == management.claim
    assert management.distillation_note not in result.items[0].content

    sales = card(
        "sales",
        default_assignment=KnowledgeAssignmentScope.MANAGER_GLOBAL.value,
    )
    result = retrieve(
        [sales],
        profile=MANAGER_RETRIEVAL_PROFILE,
        stage=RetrievalStage.REVIEW.value,
    )
    assert result.items[0].content == sales.claim
    assert sales.distillation_note not in result.items[0].content


def test_assignment_scope_is_identity_bound_and_tenant_flip_is_immediate() -> None:
    specialist_card = card("specialist")
    manager_card = card(
        "manager",
        default_assignment=KnowledgeAssignmentScope.MANAGER_GLOBAL.value,
    )
    assert [item.card.card_id for item in retrieve([specialist_card, manager_card]).items] == [
        specialist_card.card_id
    ]

    flipped = retrieve(
        [manager_card],
        assignment_overrides={
            manager_card.card_version_id: CardAssignmentOverride(
                card_version_id=manager_card.card_version_id,
                tenant_id=TENANT,
                scope="specialist:sales_recovery_agent",
            )
        },
    )
    assert [item.card.card_id for item in flipped.items] == [manager_card.card_id]

    disabled = retrieve(
        [specialist_card],
        assignment_overrides={
            specialist_card.card_version_id: CardAssignmentOverride(
                card_version_id=specialist_card.card_version_id,
                tenant_id=TENANT,
                scope=KnowledgeAssignmentScope.DISABLED.value,
                enabled=False,
            )
        },
    )
    assert disabled.items == ()

    with pytest.raises(CardRetrievalPolicyError, match="tenant did not match"):
        retrieve(
            [specialist_card],
            assignment_overrides={
                specialist_card.card_version_id: CardAssignmentOverride(
                    card_version_id=specialist_card.card_version_id,
                    tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
                    scope="specialist:sales_recovery_agent",
                )
            },
        )


def test_profiles_are_narrow_by_construction_and_retrieval_never_authorizes_effects() -> None:
    assert PROFILE.assignment_scopes == frozenset({"specialist:sales_recovery_agent"})
    assert MANAGER_RETRIEVAL_PROFILE.domains == frozenset(KnowledgeDomain)
    assert MANAGER_RETRIEVAL_PROFILE.assignment_scopes == frozenset(
        {
            KnowledgeAssignmentScope.MANAGER_GLOBAL.value,
            KnowledgeAssignmentScope.MANAGER_TENANT.value,
        }
    )
    assert CardRetrievalResult.AUTHORIZES_EFFECTS is False
    assert KnowledgeBundle.AUTHORIZES_EFFECTS is False
    assert KNOWLEDGE_RETRIEVAL_AUTHORIZES_EFFECTS is False


def test_broker_adapter_fails_closed_on_context_tenant_mismatch_and_is_not_registered() -> None:
    valid = card("adapter")
    scope = KnowledgeScope(tenant_id=TENANT, run_id=uuid4())
    adapter = CardRegistryBrokerAdapter(
        layer=KnowledgeLayer.L4,
        cards=[valid],
        card_embeddings={valid.card_version_id: [1.0, 0.0]},
        profile=PROFILE,
        domain=KnowledgeDomain.SALES,
        context_resolver=lambda _scope: context(
            tenant_id=UUID("22222222-2222-4222-8222-222222222222")
        ),
        query_embedder=lambda texts: [[1.0, 0.0] for _ in texts],
    )
    with pytest.raises(CardRetrievalPolicyError, match="tenant did not match"):
        adapter.retrieve(
            scope,
            KnowledgeQuery(
                objective="sales follow up",
                stage=RetrievalStage.SPECIALIST,
                specialist="sales_recovery_agent",
            ),
            limit=4,
        )


def test_broker_adapter_returns_redacted_provenance_bearing_global_evidence() -> None:
    valid = card("adapter-success")
    observed: list[str] = []

    def embed(texts):  # noqa: ANN001, ANN202
        observed.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    adapter = CardRegistryBrokerAdapter(
        layer=KnowledgeLayer.L4,
        cards=[valid],
        card_embeddings={valid.card_version_id: [1.0, 0.0]},
        profile=PROFILE,
        domain=KnowledgeDomain.SALES,
        context_resolver=lambda _scope: context(),
        query_embedder=embed,
    )
    results = adapter.retrieve(
        KnowledgeScope(tenant_id=TENANT, run_id=uuid4()),
        KnowledgeQuery(
            objective="Email owner@example.com about bounded sales follow up",
            stage=RetrievalStage.SPECIALIST,
            specialist="sales_recovery_agent",
        ),
        limit=4,
    )
    assert len(results) == 1
    assert results[0].tenant_id is None
    assert results[0].metadata["card_version_id"] == valid.card_version_id
    assert "owner@example.com" not in observed[0]


def test_the_engine_has_exactly_one_consumer_and_the_broker_adapter_still_has_none() -> None:
    """VT-725 opened the engine's single call site; the broker adapter stays unregistered.

    The adapter returns content-bearing ``EvidenceItem`` values into a ``KnowledgeBundle``, which
    is the injection path. ``card_serving`` deliberately bypasses it and drives the engine
    directly, returning IDs and scores only — so shadow cannot become injection by wiring.
    """

    src = Path(__file__).resolve().parents[3] / "src" / "orchestrator"
    engine_consumers = []
    adapter_consumers = []
    for path in src.rglob("*.py"):
        if path.name == "card_retrieval.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "CardRegistryBrokerAdapter" in text:
            adapter_consumers.append(path.name)
        if "knowledge.card_retrieval" in text:
            engine_consumers.append(path.name)
    assert adapter_consumers == []
    assert engine_consumers == ["card_serving.py"]


def test_in_memory_retrieval_latency_is_observed_not_wall_clock_gated(
    record_property: Callable[[str, object], None],
) -> None:
    cards = [card(f"latency-{index}") for index in range(500)]
    result = retrieve(cards, top_k=8)
    assert len(result.items) == 8
    assert result.trace.elapsed_ms >= 0.0
    # Shared CI runner load makes a fixed wall-clock limit permanently flaky. Preserve the
    # measurement as advisory test output; performance regression gates belong in a controlled
    # benchmark environment with a declared baseline.
    record_property("o8_retrieval_500_cards_elapsed_ms", round(result.trace.elapsed_ms, 3))
