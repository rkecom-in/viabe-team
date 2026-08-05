"""VT-725 tests for the O8 card-serving consumer.

Pure: no database and no embedding provider.  The connection is a stub that applies the same
predicates the real SQL applies (domain/scope/tenant), so the budget the serving layer declares
is PROVEN to reach the query rather than merely asserted on a parameter list.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

pytest.importorskip("pydantic")

from orchestrator.agent_framework.retrieval_profiles import (  # noqa: E402
    MANAGER_RETRIEVAL_PROFILE,
    SPECIALIST_RETRIEVAL_PROFILES,
    retrieval_profile_for,
)
from orchestrator.knowledge import card_serving  # noqa: E402
from orchestrator.knowledge.card_retrieval import RetrievalBusinessContext  # noqa: E402
from orchestrator.knowledge.card_serving import (  # noqa: E402
    CardServingResult,
    KnowledgeServingMode,
    ServedCardRef,
    declared_budget,
    embed_cards,
    load_serving_corpus,
    record_evidence_links,
    resolve_business_context,
    retrieve_cards_for_turn,
    serving_mode,
)
from orchestrator.knowledge.contracts import KnowledgeDomain, RetrievalStage  # noqa: E402

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
RUN = UUID("33333333-3333-4333-8333-333333333333")
CORPUS = "44444444-4444-4444-8444-444444444444"

#: The engine scores lexical overlap between the objective and claim+note+claim_key, so the
#: fixtures deliberately share vocabulary with this objective — a test corpus that scored below
#: the profile minimum would prove nothing about serving.
OBJECTIVE = "bounded follow up improves response discipline"


def card_row(
    card_uuid: str,
    *,
    domain: str = "sales",
    default_assignment: str = "manager_global",
    scope: str = "global",
    status: str = "validated",
    source_class: str = "t2",
    authority: str = "seed",
    retrieval_eligible: bool = True,
    corpus_version_id: str | None = CORPUS,
    cluster: str | None = None,
    claim_key: str = "sales_follow_up|improves_response_discipline|in|small|whatsapp",
    claim_value: dict[str, Any] | None = None,
    usage_rights: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Migration 189's single-row projection, exactly as the VT-726 seed writer persists it."""

    return {
        "id": card_uuid,
        "card_key": f"key-{card_uuid}",
        "version": 1,
        "claim": OBJECTIVE,
        "claim_key": claim_key,
        "claim_value": claim_value or {"value_type": "text", "value": "keep follow-up bounded"},
        "distillation_note": OBJECTIVE,
        "jurisdictions": ["IN"],
        "size_bands": ["small"],
        "industries": ["retail"],
        "maturity_stages": ["growth"],
        "channels": ["whatsapp"],
        "applicability_universal": False,
        "effective_from": NOW - timedelta(days=30),
        "effective_until": None,
        "authority": authority,
        "confidence": "high",
        "scope": scope,
        "status": status,
        "retention_class": "lifecycle_managed",
        "expires_at": expires_at,
        "default_assignment": default_assignment,
        "domain": domain,
        "source_class": source_class,
        "usage_rights": usage_rights
        or {
            "status": "permission_granted",
            "allows_extraction": True,
            "allows_embedding": True,
            "allows_retrieval": True,
            "reviewed_at": "2026-07-27T12:00:00+00:00",
            "reviewed_by": "test",
        },
        "independence_cluster": cluster or f"cluster-{card_uuid}",
        "corroboration_cluster_count": 2,
        "provenance": provenance
        or {
            "source_ids": [f"src-{card_uuid}"],
            "publisher": "test publisher",
            "retrieved_at": "2026-07-04T12:00:00+00:00",
            "tainted": True,
        },
        "retrieval_eligible": retrieval_eligible,
        "corpus_version_id": corpus_version_id,
    }


class FakeCursor:
    def __init__(self, sink: list[tuple[str, list[Any]]]) -> None:
        self._sink = sink
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def executemany(self, sql: str, rows: Any) -> None:
        materialized = list(rows)
        self._sink.append((sql, materialized))
        self.rowcount = len(materialized)


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class FakeConn:
    """Applies the real query's predicates so a budget that never reaches SQL fails a test."""

    def __init__(
        self,
        *,
        cards: list[dict[str, Any]] | None = None,
        assignments: dict[str, list[dict[str, Any]]] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.cards = cards or []
        self.assignments = assignments or {}
        self.raise_on = raise_on
        self.queries: list[tuple[str, Any]] = []
        self.written: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: Any = None) -> FakeResult:
        self.queries.append((sql, params))
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("injected database failure")
        if "FROM knowledge_card_assignments" in sql:
            return FakeResult(list(self.assignments.get(str(params[0]), [])))
        if "FROM knowledge_cards" in sql:
            domains, scopes = params[0], params[1]
            return FakeResult(
                [
                    row
                    for row in self.cards
                    if row["retrieval_eligible"]
                    and row["status"] in {"validated", "disputed"}
                    and row["domain"] in domains
                    and row["scope"] in scopes
                ]
            )
        raise AssertionError(f"unexpected query: {sql[:60]}")

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.written)


def context(tenant_id: UUID = TENANT_A) -> RetrievalBusinessContext:
    return RetrievalBusinessContext(
        tenant_id=tenant_id,
        jurisdiction="IN",
        size_band="small",
        industry="retail",
        maturity_stage="growth",
        channel="whatsapp",
        as_of=NOW,
    )


@pytest.fixture(autouse=True)
def _shadow_mode_and_stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_KNOWLEDGE_SERVING", "shadow")
    monkeypatch.setattr(card_serving, "embed_query", lambda objective: [1.0, 0.0])
    monkeypatch.setattr(
        card_serving,
        "embed_cards",
        lambda cards, **_: ({card.card_version_id: [1.0, 0.0] for card in cards}, ()),
    )
    card_serving._EMBED_CACHE.clear()


def serve(conn: FakeConn, **updates: Any) -> CardServingResult:
    values: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "run_id": RUN,
        "decision_id": "turn-1",
        "objective": OBJECTIVE,
        "stage": RetrievalStage.PLANNING,
        "domain": KnowledgeDomain.SALES,
        "context": context(),
        "conn": conn,
    }
    values.update(updates)
    return retrieve_cards_for_turn(**values)


def test_shadow_retrieves_and_scores_but_carries_no_card_content_anywhere() -> None:
    conn = FakeConn(cards=[card_row("card-a")])
    result = serve(conn)

    assert result.mode is KnowledgeServingMode.SHADOW
    assert result.selected_card_refs == ("card-a",)
    assert result.no_result is False

    # The guarantee is structural, not a convention: there is no content-bearing surface to
    # inject FROM, so a caller cannot reach card text even by accident.
    assert CardServingResult.INJECTS_INTO_PROMPT is False
    assert CardServingResult.AUTHORIZES_EFFECTS is False
    rendered = repr(dataclasses.asdict(result))
    assert OBJECTIVE not in rendered
    assert "keep follow-up bounded" not in rendered
    field_names = {field.name for field in dataclasses.fields(CardServingResult)}
    assert field_names.isdisjoint({"content", "cards", "items", "prompt", "block", "text"})
    assert {field.name for field in dataclasses.fields(ServedCardRef)} == {
        "card_version_ref",
        "disposition",
        "corpus_version_ref",
        "semantic_score",
        "lexical_score",
        "entity_score",
        "combined_score",
    }


def test_serving_is_off_by_default_and_active_is_not_env_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_KNOWLEDGE_SERVING", raising=False)
    assert serving_mode() is KnowledgeServingMode.OFF
    for attempted in ("active", "ACTIVE", "on", "1", "true"):
        monkeypatch.setenv("TEAM_KNOWLEDGE_SERVING", attempted)
        assert serving_mode() is KnowledgeServingMode.OFF

    # Off must not touch the database at all — not a load, not an attribution write.
    conn = FakeConn(cards=[card_row("card-a")], raise_on="SELECT")
    result = serve(conn)
    assert result.degraded_reason == "serving_off"
    assert result.refs == ()
    assert conn.queries == []
    assert conn.written == []


def test_evidence_links_carry_retrieved_selected_and_rejected_for_the_same_decision() -> None:
    # 'kept' clears the profile minimum; 'dropped' shares its independence cluster and claim so
    # the engine's dedupe removes it — a real rejection, not a fabricated one.
    conn = FakeConn(
        cards=[
            card_row("card-kept", cluster="shared-cluster"),
            card_row("card-shadowed", cluster="shared-cluster"),
        ]
    )
    result = serve(conn)

    dispositions = {(ref.card_version_ref, ref.disposition) for ref in result.refs}
    assert ("card-kept", "retrieved") in dispositions
    assert ("card-kept", "selected") in dispositions
    assert ("card-shadowed", "retrieved") in dispositions
    assert ("card-shadowed", "rejected") in dispositions
    assert ("card-shadowed", "selected") not in dispositions

    assert result.evidence_links_written == len(result.refs)
    assert result.evidence_links_error is None
    _, rows = conn.written[0]
    assert {row[8] for row in rows} == {"retrieved", "selected", "rejected"}
    # Every row is ids + stage + scores. Nothing carries claim text or tenant narrative.
    for row in rows:
        assert row[0] == str(TENANT_A)
        assert row[4] == CORPUS and row[6] in {"card-kept", "card-shadowed"}
        assert row[7] == RetrievalStage.PLANNING.value
        assert all(value is None or 0.0 <= value <= 1.0 for value in row[9:13])
    selected_rows = [row for row in rows if row[8] == "selected"]
    assert all(row[12] is not None for row in selected_rows)
    rejected_rows = [row for row in rows if row[8] == "rejected"]
    assert all(row[12] is None for row in rejected_rows)


def test_per_tenant_override_changes_retrieval_for_that_tenant_only() -> None:
    card = card_row("card-flippable", default_assignment="manager_global")
    flip_away = {
        str(TENANT_A): [
            {"card_id": "card-flippable", "scope": "specialist:sales_recovery_agent",
             "enabled": True}
        ]
    }

    conn_a = FakeConn(cards=[card], assignments=flip_away)
    flipped = serve(conn_a, tenant_id=TENANT_A, context=context(TENANT_A))
    conn_b = FakeConn(cards=[card], assignments=flip_away)
    untouched = serve(conn_b, tenant_id=TENANT_B, context=context(TENANT_B))

    # Same global card, same corpus, same turn: the tenant override alone decides.
    assert flipped.selected_card_refs == ()
    assert untouched.selected_card_refs == ("card-flippable",)

    # A disabled override is the other half of the flip and must also be tenant-local.
    disable = {
        str(TENANT_A): [
            {"card_id": "card-flippable", "scope": "disabled", "enabled": False}
        ]
    }
    assert serve(
        FakeConn(cards=[card], assignments=disable), tenant_id=TENANT_A
    ).selected_card_refs == ()
    assert serve(
        FakeConn(cards=[card], assignments=disable), tenant_id=TENANT_B, context=context(TENANT_B)
    ).selected_card_refs == ("card-flippable",)


def test_an_override_can_grant_a_specialist_a_card_the_manager_held_by_default() -> None:
    # The other half of the runtime flip: assignment moves a card BETWEEN estates, it does not
    # only remove it. Same card, same corpus, no deploy.
    card = card_row("card-flippable", default_assignment="manager_global")
    grant = {
        str(TENANT_A): [
            {"card_id": "card-flippable", "scope": "specialist:sales_recovery_agent",
             "enabled": True}
        ]
    }
    specialist_kwargs: dict[str, Any] = {
        "identity": "sales_recovery_agent",
        "stage": RetrievalStage.SPECIALIST,
    }

    assert serve(
        FakeConn(cards=[card]), **specialist_kwargs
    ).selected_card_refs == ()
    assert serve(
        FakeConn(cards=[card], assignments=grant), **specialist_kwargs
    ).selected_card_refs == ("card-flippable",)
    assert serve(
        FakeConn(cards=[card], assignments=grant), tenant_id=TENANT_B, context=context(TENANT_B),
        **specialist_kwargs,
    ).selected_card_refs == ()


def test_a_context_for_a_different_tenant_is_refused_rather_than_served() -> None:
    conn = FakeConn(cards=[card_row("card-a")])
    result = serve(conn, tenant_id=TENANT_A, context=context(TENANT_B))

    assert result.refs == ()
    assert result.degraded_reason == "error:ValueError"
    assert conn.written == []


def test_specialist_retrieval_is_narrow_in_both_domain_and_assignment() -> None:
    lane = card_row(
        "card-lane", domain="sales", default_assignment="specialist:sales_recovery_agent"
    )
    other_lane = card_row(
        "card-compliance", domain="compliance",
        default_assignment="specialist:sales_recovery_agent",
    )
    manager_only = card_row("card-manager", domain="sales", default_assignment="manager_global")
    other_specialist = card_row(
        "card-onboarding", domain="sales", default_assignment="specialist:onboarding_conductor"
    )
    conn = FakeConn(cards=[lane, other_lane, manager_only, other_specialist])

    result = serve(
        conn,
        identity="sales_recovery_agent",
        stage=RetrievalStage.SPECIALIST,
        domain=KnowledgeDomain.SALES,
    )

    assert result.selected_card_refs == ("card-lane",)
    # Out-of-lane cards never even enter the candidate pool: the declared corpus domains are a
    # SQL predicate, so a compliance card is not merely out-ranked for a sales specialist.
    candidate_refs = {ref.card_version_ref for ref in result.refs}
    assert "card-compliance" not in candidate_refs
    assert candidate_refs == {"card-lane", "card-manager", "card-onboarding"}

    card_query = next(sql for sql, _ in conn.queries if "FROM knowledge_cards" in sql)
    params = next(params for sql, params in conn.queries if sql == card_query)
    assert "FROM knowledge_corpus_versions" in card_query
    assert "JOIN knowledge_corpus_members" in card_query
    assert "ORDER BY version DESC" in card_query
    assert "serving_corpus.id AS corpus_version_id" in card_query
    assert "LEFT JOIN knowledge_card_embeddings" in card_query
    assert params[0] == ["marketing", "sales"]
    assert params[-1] == card_serving._MAX_CANDIDATE_CARDS

    # The Manager, on the same corpus, sees the breadth the specialist does not.
    manager = serve(FakeConn(cards=[lane, other_lane, manager_only, other_specialist]))
    assert manager.selected_card_refs == ("card-manager",)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("query_embedding", "error:RuntimeError"),
        ("corpus_load", "error:RuntimeError"),
        ("unknown_identity", "error:KeyError"),
    ],
)
def test_every_retrieval_failure_degrades_to_no_cards_and_the_turn_completes(
    monkeypatch: pytest.MonkeyPatch, failure: str, expected: str
) -> None:
    conn = FakeConn(cards=[card_row("card-a")])
    kwargs: dict[str, Any] = {}
    if failure == "query_embedding":
        def _boom(objective: str) -> list[float]:
            raise RuntimeError("voyage key absent")

        monkeypatch.setattr(card_serving, "embed_query", _boom)
    elif failure == "corpus_load":
        conn = FakeConn(cards=[card_row("card-a")], raise_on="FROM knowledge_cards")
    else:
        kwargs["identity"] = "no_such_agent"

    result = serve(conn, **kwargs)

    assert result.refs == ()
    assert result.no_result is True
    assert result.degraded_reason == expected
    assert result.evidence_links_written == 0
    assert conn.written == []


def test_an_unembeddable_card_is_excluded_rather_than_failing_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        card_serving,
        "embed_cards",
        lambda cards, **_: (
            {card.card_version_id: [1.0, 0.0] for card in cards if card.card_version_id != "card-b"},
            ("card-b",),
        ),
    )
    result = serve(FakeConn(cards=[card_row("card-a"), card_row("card-b")]))

    assert result.selected_card_refs == ("card-a",)
    assert {ref.card_version_ref for ref in result.refs} == {"card-a"}
    assert result.degraded_reason == "cards_unembeddable:1"


def test_a_card_whose_embedding_dimension_disagrees_with_the_query_is_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        card_serving,
        "embed_cards",
        lambda cards, **_: (
            {
                card.card_version_id: ([1.0, 0.0] if card.card_version_id == "card-a" else [1.0])
                for card in cards
            },
            (),
        ),
    )
    result = serve(FakeConn(cards=[card_row("card-a"), card_row("card-short")]))

    assert result.selected_card_refs == ("card-a",)
    assert result.candidates == 1


def test_attribution_is_skipped_loudly_for_a_card_outside_any_corpus_version() -> None:
    conn = FakeConn(
        cards=[card_row("card-in", corpus_version_id=CORPUS),
               card_row("card-out", corpus_version_id=None, cluster="other")]
    )
    result = serve(conn)

    # Both cards still serve; only the un-versioned card's attribution cannot be written, and the
    # result says how many rows that cost instead of reporting a quieter number.
    assert set(result.selected_card_refs) == {"card-in", "card-out"}
    assert result.corpus_version_refs == (CORPUS,)
    assert result.evidence_links_error == "cards_without_corpus_version:2"
    _, rows = conn.written[0]
    assert {row[6] for row in rows} == {"card-in"}


def test_a_card_the_curator_marked_ineligible_never_serves() -> None:
    # retrieval_eligible is the pipeline's admission decision (migration 189). The consumer
    # honours it as a predicate rather than re-deriving eligibility from status.
    conn = FakeConn(
        cards=[card_row("card-live"), card_row("card-parked", retrieval_eligible=False)]
    )
    result = serve(conn)

    assert result.selected_card_refs == ("card-live",)
    assert {ref.card_version_ref for ref in result.refs} == {"card-live"}
    card_query = next(sql for sql, _ in conn.queries if "FROM knowledge_cards" in sql)
    assert "c.retrieval_eligible" in card_query


def test_registry_rows_that_cannot_satisfy_the_governance_contract_are_dropped() -> None:
    ungovernable = [
        card_row("no-sources", provenance={"publisher": "p", "retrieved_at": "2026-07-04T12:00:00+00:00"}),
        card_row("bad-claim-key", claim_key="subject|predicate|in"),
        card_row("bad-authority", authority="not_an_authority"),
        # A T4 experiential card without the mandatory six-month expiry is exactly the kind of
        # row the contract exists to keep out of reasoning.
        card_row("t4-no-expiry", source_class="t4"),
    ]
    corpus = load_serving_corpus(
        TENANT_A,
        MANAGER_RETRIEVAL_PROFILE,
        conn=FakeConn(cards=[*ungovernable, card_row("card-good")]),
    )

    assert [card.card_version_id for card in corpus.cards] == ["card-good"]
    assert corpus.unmappable_rows == len(ungovernable)
    assert corpus.cards[0].corpus_version_id == CORPUS


def test_a_thinned_or_truncated_pool_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(card_serving, "_MAX_CANDIDATE_CARDS", 2)
    conn = FakeConn(cards=[card_row("card-a"), card_row("card-b"), card_row("bad", provenance={"source_ids": [], "publisher": "p", "retrieved_at": "x"})])
    result = serve(conn)

    # "No cards" because the corpus had nothing applicable and "no cards" because rows were
    # dropped or the pool was cut off must never read the same to whoever inspects the shadow.
    assert result.degraded_reason is not None
    assert "rows_unmappable:1" in result.degraded_reason
    assert "candidate_pool_truncated" in result.degraded_reason


def test_registry_rows_map_onto_the_governed_contract() -> None:
    corpus = load_serving_corpus(
        TENANT_A,
        MANAGER_RETRIEVAL_PROFILE,
        conn=FakeConn(
            cards=[
                card_row(
                    "card-a",
                    claim_value={"value_type": "decimal", "value": "12.5", "unit": "percent"},
                    # Unknown rights must not cost a card its place (CL-2026-07-29b).
                    usage_rights={"status": "unknown"},
                )
            ]
        ),
    )

    card = corpus.cards[0]
    assert card.card_version_id == "card-a" and card.card_id == "key-card-a"
    assert card.corpus_version_id == CORPUS
    assert card.claim_key.canonical == (
        "sales_follow_up|improves_response_discipline|in|small|whatsapp"
    )
    assert str(card.claim_value.value) == "12.5" and card.claim_value.unit == "percent"
    assert card.usage_rights.status.value == "unknown"
    assert card.retrieval_eligible is True
    assert card.applicability.jurisdictions == ("IN",)
    assert card.provenance.source_ids == ("src-card-a",)


def test_a_malformed_assignment_override_falls_back_to_the_narrower_global_default() -> None:
    corpus = load_serving_corpus(
        TENANT_A,
        MANAGER_RETRIEVAL_PROFILE,
        conn=FakeConn(
            cards=[card_row("card-a")],
            assignments={
                str(TENANT_A): [
                    {"card_id": "card-a", "scope": "everyone", "enabled": True},
                    {"card_id": "card-b", "scope": "manager_tenant", "enabled": True},
                ]
            },
        ),
    )

    assert "card-a" not in corpus.overrides
    assert corpus.overrides["card-b"].scope == "manager_tenant"


def test_business_context_read_failure_yields_a_thin_context_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(tenant_id: Any) -> Any:
        raise RuntimeError("l1 unavailable")

    monkeypatch.setattr(
        "orchestrator.knowledge.business_context.read_business_context", _boom
    )
    resolved = resolve_business_context(TENANT_A)

    # Unknown dimensions hedge in the engine; they never exclude. Jurisdiction is the exception
    # and stays pinned, so a foreign regulatory card cannot ride in on an unknown.
    assert resolved.tenant_id == TENANT_A
    assert resolved.jurisdiction == "IN"
    assert resolved.channel == "whatsapp"
    assert (resolved.size_band, resolved.industry, resolved.maturity_stage) == (None, None, None)


def test_declared_budget_is_the_specialist_lane_not_the_manager_breadth() -> None:
    manager = declared_budget(retrieval_profile_for("team_manager"))
    specialist = declared_budget(SPECIALIST_RETRIEVAL_PROFILES["sales_recovery_agent"])

    assert manager.depth == "conclusions"
    assert set(specialist.domains) < set(manager.domains)
    assert specialist.assignment_scopes == ("specialist:sales_recovery_agent",)
    assert manager.assignment_scopes == ("manager_global", "manager_tenant")
    assert specialist.top_k <= 20 and specialist.token_budget <= 12_000


def test_card_embeddings_are_cached_per_immutable_card_version() -> None:
    calls: list[list[str]] = []

    def _embedder(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    corpus = load_serving_corpus(
        TENANT_A, MANAGER_RETRIEVAL_PROFILE, conn=FakeConn(cards=[card_row("card-a")])
    )
    import orchestrator.knowledge.embeddings as embeddings_module

    original = embeddings_module.embed_redacted_texts
    embeddings_module.embed_redacted_texts = _embedder  # type: ignore[assignment]
    try:
        first, failed_first = embed_cards(corpus.cards)
        second, failed_second = embed_cards(corpus.cards)
    finally:
        embeddings_module.embed_redacted_texts = original  # type: ignore[assignment]

    assert first == second and failed_first == failed_second == ()
    assert len(calls) == 1  # a card row can never change, so one embed per version is enough


def test_persisted_embedding_survives_process_cache_and_avoids_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = load_serving_corpus(
        TENANT_A, MANAGER_RETRIEVAL_PROFILE, conn=FakeConn(cards=[card_row("card-a")])
    )
    card = corpus.cards[0]
    card_serving._EMBED_CACHE.clear()

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("persisted embedding should avoid provider egress")

    monkeypatch.setattr(
        "orchestrator.knowledge.embeddings.embed_redacted_texts", _must_not_call
    )
    vectors, failed = embed_cards(
        (card,), persisted={card.card_version_id: [1.0, 0.0]}
    )
    assert vectors == {card.card_version_id: [1.0, 0.0]}
    assert failed == ()


def test_evidence_link_write_failure_is_reported_not_raised() -> None:
    class ExplodingConn(FakeConn):
        def cursor(self) -> Any:
            raise RuntimeError("connection lost mid-write")

    written, error = record_evidence_links(
        TENANT_A,
        run_id=RUN,
        decision_id="turn-1",
        stage=RetrievalStage.PLANNING,
        refs=(
            ServedCardRef(
                card_version_ref="card-a", disposition="retrieved", corpus_version_ref=CORPUS
            ),
        ),
        conn=ExplodingConn(),
    )

    assert (written, error) == (0, "error:RuntimeError")
