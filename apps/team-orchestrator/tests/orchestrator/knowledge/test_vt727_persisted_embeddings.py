"""VT-727 immutable, model-pinned persisted embedding behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.contracts import KnowledgeCard
from orchestrator.knowledge.embeddings import EMBED_DIM, EMBED_MODEL
from orchestrator.knowledge.persisted_embeddings import (
    EmbeddingStoreError,
    bind_embeddings,
    card_content_digest,
    persist_embeddings,
)


def _card() -> KnowledgeCard:
    from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit

    root = Path(__file__).resolve().parents[5]
    corpus = root / "apps" / "team-orchestrator" / "knowledge_corpus"
    load = lambda name: [  # noqa: E731 — compact fixture loader
        json.loads(line)
        for line in (corpus / name).read_text(encoding="utf-8").splitlines()
        if line
    ]
    audit = load_independence_audit(
        json.loads((corpus / "independence_audit.json").read_text(encoding="utf-8"))
    )
    return (
        build_full_plan(load("source_rights.jsonl"), load("candidate_cards.jsonl"), audit)
        .cards[0]
        .representative
    )


def test_binding_pins_immutable_card_content_model_and_dimensions() -> None:
    card = _card()
    vector = [0.125] * EMBED_DIM
    bound = bind_embeddings((card,), {card.card_version_id: vector})
    assert len(bound) == 1
    assert bound[0].model == EMBED_MODEL
    assert bound[0].dimensions == EMBED_DIM
    assert bound[0].content_digest == card_content_digest(card)
    assert bound[0].AUTHORIZES_EFFECTS is False


@pytest.mark.parametrize(
    "vectors",
    [
        {},
        {"extra": [0.0] * EMBED_DIM},
    ],
)
def test_binding_rejects_missing_or_extra_provider_output(vectors) -> None:
    with pytest.raises(EmbeddingStoreError, match="mismatch"):
        bind_embeddings((_card(),), vectors)


def test_binding_rejects_wrong_dimension_and_non_finite_values() -> None:
    card = _card()
    with pytest.raises(EmbeddingStoreError, match="invalid"):
        bind_embeddings((card,), {card.card_version_id: [0.0] * (EMBED_DIM - 1)})
    with pytest.raises(EmbeddingStoreError, match="invalid"):
        bind_embeddings((card,), {card.card_version_id: [float("nan")] * EMBED_DIM})


class Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))


def test_persistence_is_insert_only_and_pgvector_casted() -> None:
    card = _card()
    item = bind_embeddings((card,), {card.card_version_id: [0.25] * EMBED_DIM})[0]
    conn = Conn()
    persist_embeddings(conn, (item,))
    assert len(conn.calls) == 1
    query, params = conn.calls[0]
    assert "INSERT INTO public.knowledge_card_embeddings" in query
    assert "%s::vector" in query
    assert "UPDATE" not in query
    assert params[0] == card.card_version_id
    assert params[1:4] == (EMBED_MODEL, EMBED_DIM, card_content_digest(card))

    with pytest.raises(EmbeddingStoreError, match="drift"):
        persist_embeddings(conn, (replace(item, model="wrong-model"),))
