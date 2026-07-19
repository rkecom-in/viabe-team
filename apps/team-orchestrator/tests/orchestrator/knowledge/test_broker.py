from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

# The knowledge broker/contracts are pydantic-backed — SKIP cleanly in the dep-less smoke env
# (pytest + pyyaml only), which the pre-push hook runs across the whole tree. Without this the
# module fails collection on `import pydantic` and aborts the push.
pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from orchestrator.knowledge.broker import (  # noqa: E402
    KnowledgeBroker,
    KnowledgeContractError,
    KnowledgeIsolationError,
)
from orchestrator.knowledge.contracts import (  # noqa: E402
    EvidenceAuthority,
    EvidenceConfidence,
    EvidenceItem,
    KnowledgeLayer,
    KnowledgeQuery,
    KnowledgeScope,
    MemoryKind,
    RetrievalStage,
    SpecialistName,
)


TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


@dataclass
class StubAdapter:
    layer: KnowledgeLayer
    items: list[EvidenceItem] = field(default_factory=list)
    error: Exception | None = None
    calls: int = 0

    def retrieve(
        self,
        scope: KnowledgeScope,
        query: KnowledgeQuery,
        *,
        limit: int,
    ) -> list[EvidenceItem]:
        del scope, query
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.items[:limit]


def scope() -> KnowledgeScope:
    return KnowledgeScope(tenant_id=TENANT_ID, run_id=uuid4())


def query(**updates: object) -> KnowledgeQuery:
    values: dict[str, object] = {
        "objective": "Recover sales from customers who have become inactive",
        "stage": RetrievalStage.PLANNING,
        "token_budget": 2_500,
    }
    values.update(updates)
    return KnowledgeQuery.model_validate(values)


def evidence(
    evidence_id: str,
    *,
    layer: KnowledgeLayer = KnowledgeLayer.L1,
    kind: MemoryKind = MemoryKind.FACT,
    authority: EvidenceAuthority = EvidenceAuthority.VERIFIED_SYSTEM,
    tenant_id: UUID | None = TENANT_ID,
    source_id: str | None = None,
    content: str | None = None,
    score: float | None = None,
    occurred_at: datetime | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    retrieval_eligible: bool = True,
    superseded_by: str | None = None,
    claim_key: str | None = None,
    claim_value: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        layer=layer,
        kind=kind,
        authority=authority,
        source_id=source_id or evidence_id,
        content=content or f"Evidence content for {evidence_id}",
        score=score,
        occurred_at=occurred_at,
        valid_from=valid_from,
        valid_to=valid_to,
        confidence=EvidenceConfidence.VERIFIED,
        retrieval_eligible=retrieval_eligible,
        superseded_by=superseded_by,
        claim_key=claim_key,
        claim_value=claim_value,
    )


def test_query_has_no_model_supplied_tenant_and_requires_specialist() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeQuery.model_validate(
            {
                "objective": "Connect my store",
                "stage": "planning",
                "tenant_id": str(TENANT_ID),
            }
        )

    with pytest.raises(ValidationError, match="specialist is required"):
        query(stage=RetrievalStage.SPECIALIST)

    specialist_query = query(
        stage=RetrievalStage.SPECIALIST,
        specialist=SpecialistName.INTEGRATION,
    )
    assert specialist_query.specialist == SpecialistName.INTEGRATION

    with pytest.raises(ValidationError):
        query(objective="   ")
    with pytest.raises(ValidationError):
        query(layers=frozenset())


def test_tenant_id_is_used_for_validation_but_excluded_from_serialization() -> None:
    item = evidence("fact-1")

    assert item.tenant_id == TENANT_ID
    assert "tenant_id" not in item.model_dump()


@pytest.mark.parametrize("bad_tenant", [OTHER_TENANT_ID, None])
def test_tenant_scoped_adapter_mismatch_fails_closed(
    bad_tenant: UUID | None,
) -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L1,
        [evidence("foreign", tenant_id=bad_tenant)],
    )

    with pytest.raises(KnowledgeIsolationError, match="did not match runtime scope"):
        KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)


def test_global_layer_must_not_carry_tenant_identity() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L3,
        [
            evidence(
                "prior",
                layer=KnowledgeLayer.L3,
                tenant_id=TENANT_ID,
            )
        ],
    )

    with pytest.raises(KnowledgeIsolationError, match="carried tenant identity"):
        KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)


def test_adapter_cannot_return_a_different_layer() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L1,
        [evidence("wrong-layer", layer=KnowledgeLayer.L2)],
    )

    with pytest.raises(KnowledgeContractError, match="returned l2 evidence"):
        KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)


def test_authority_precedes_similarity_and_results_are_categorized() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L1,
        [
            evidence(
                "seed-high-score",
                authority=EvidenceAuthority.SEED,
                score=1.0,
                kind=MemoryKind.SEED,
            ),
            evidence(
                "owner-low-score",
                authority=EvidenceAuthority.OWNER,
                score=0.1,
                kind=MemoryKind.POLICY,
            ),
            evidence(
                "system-fact",
                authority=EvidenceAuthority.VERIFIED_SYSTEM,
                score=0.8,
            ),
            evidence(
                "relationship",
                authority=EvidenceAuthority.VERIFIED_SYSTEM,
                kind=MemoryKind.RELATIONSHIP,
            ),
            evidence(
                "outcome",
                authority=EvidenceAuthority.VERIFIED_OUTCOME,
                kind=MemoryKind.OUTCOME,
            ),
        ],
    )

    bundle = KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)

    assert bundle.evidence_manifest == (
        "owner-low-score",
        "system-fact",
        "relationship",
        "outcome",
        "seed-high-score",
    )
    assert [item.evidence_id for item in bundle.facts] == ["system-fact"]
    assert [item.evidence_id for item in bundle.relationships] == ["relationship"]
    assert [item.evidence_id for item in bundle.episodes] == ["outcome"]
    assert [item.evidence_id for item in bundle.policies_and_lessons] == [
        "owner-low-score",
        "seed-high-score",
    ]
    assert tuple(item.evidence_id for item in bundle.items) == bundle.evidence_manifest


def test_filters_invalid_memories_and_deduplicates_by_layer_and_source() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L2,
        [
            evidence(
                "expired",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                valid_to=NOW,
            ),
            evidence(
                "future",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                valid_from=NOW + timedelta(seconds=1),
            ),
            evidence(
                "superseded",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                superseded_by="replacement",
            ),
            evidence(
                "not-eligible",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                retrieval_eligible=False,
            ),
            evidence(
                "duplicate-weaker",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                source_id="same-event",
                authority=EvidenceAuthority.SEED,
            ),
            evidence(
                "duplicate-winner",
                layer=KnowledgeLayer.L2,
                kind=MemoryKind.EPISODE,
                source_id="same-event",
                authority=EvidenceAuthority.VERIFIED_OUTCOME,
            ),
        ],
    )

    bundle = KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)

    assert bundle.evidence_manifest == ("duplicate-winner",)
    assert set(bundle.trace.omitted_evidence_ids) == {
        "expired",
        "future",
        "superseded",
        "not-eligible",
        "duplicate-weaker",
    }


def test_conflicting_claims_are_preserved_and_reported() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L1,
        [
            evidence(
                "owner-hours",
                authority=EvidenceAuthority.OWNER,
                claim_key="business.working_hours.monday",
                claim_value="10:00-19:00",
            ),
            evidence(
                "system-hours",
                authority=EvidenceAuthority.VERIFIED_SYSTEM,
                claim_key="business.working_hours.monday",
                claim_value="09:00-18:00",
            ),
        ],
    )

    bundle = KnowledgeBroker([adapter]).retrieve(scope(), query(), now=NOW)

    assert bundle.evidence_manifest == ("owner-hours", "system-hours")
    assert len(bundle.conflicts) == 1
    assert bundle.conflicts[0].claim_key == "business.working_hours.monday"
    assert bundle.conflicts[0].evidence_ids == ("owner-hours", "system-hours")


def test_ordinary_adapter_failure_is_fail_soft_without_error_content() -> None:
    working = StubAdapter(KnowledgeLayer.L1, [evidence("available")])
    failed = StubAdapter(KnowledgeLayer.L2, error=RuntimeError("secret vendor detail"))

    bundle = KnowledgeBroker([working, failed]).retrieve(scope(), query(), now=NOW)

    assert bundle.evidence_manifest == ("available",)
    assert bundle.trace.adapter_errors == {"l2": "RuntimeError"}
    assert "secret vendor detail" not in str(bundle.trace)


def test_layer_selection_avoids_calling_unrequested_adapters() -> None:
    l1 = StubAdapter(KnowledgeLayer.L1, [evidence("l1")])
    l4 = StubAdapter(
        KnowledgeLayer.L4,
        [evidence("l4", layer=KnowledgeLayer.L4, tenant_id=None)],
    )

    bundle = KnowledgeBroker([l1, l4]).retrieve(
        scope(), query(layers=frozenset({KnowledgeLayer.L1})), now=NOW
    )

    assert bundle.evidence_manifest == ("l1",)
    assert l1.calls == 1
    assert l4.calls == 0
    assert bundle.trace.layers_queried == (KnowledgeLayer.L1,)


def test_token_budget_truncates_one_item_and_omits_the_rest() -> None:
    adapter = StubAdapter(
        KnowledgeLayer.L1,
        [
            evidence(
                "large-owner-policy",
                authority=EvidenceAuthority.OWNER,
                kind=MemoryKind.POLICY,
                content="A" * 2_000,
            ),
            evidence("later-fact", content="B" * 100),
        ],
    )

    bundle = KnowledgeBroker([adapter]).retrieve(
        scope(), query(token_budget=256), now=NOW
    )

    assert bundle.token_count == 256
    assert bundle.evidence_manifest == ("large-owner-policy",)
    assert bundle.policies_and_lessons[0].content.endswith("[truncated]")
    assert bundle.policies_and_lessons[0].metadata["content_truncated"] is True
    assert "later-fact" in bundle.trace.omitted_evidence_ids


def test_duplicate_adapter_registration_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate adapter"):
        KnowledgeBroker(
            [
                StubAdapter(KnowledgeLayer.L1),
                StubAdapter(KnowledgeLayer.L1),
            ]
        )


def test_duplicate_evidence_ids_are_rejected_even_across_layers() -> None:
    l1 = StubAdapter(KnowledgeLayer.L1, [evidence("ambiguous")])
    l3 = StubAdapter(
        KnowledgeLayer.L3,
        [evidence("ambiguous", layer=KnowledgeLayer.L3, tenant_id=None)],
    )

    with pytest.raises(KnowledgeContractError, match="duplicate evidence_id"):
        KnowledgeBroker([l1, l3]).retrieve(scope(), query(), now=NOW)


def test_evidence_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        evidence("naive", occurred_at=datetime(2026, 7, 5, 12, 0))


def test_claim_key_and_value_must_be_supplied_together() -> None:
    with pytest.raises(ValidationError, match="must be supplied together"):
        evidence("invalid-claim", claim_key="business.type")
