from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.registry_seed import build_seed_plan
from orchestrator.knowledge.shadow_embeddings import (
    clear_shadow_embedding_cache,
    embed_cards_fail_soft,
    embed_query_fail_soft,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps/team-orchestrator/knowledge_corpus"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def cards():
    clear_shadow_embedding_cache()
    plan = build_seed_plan(
        _jsonl(CORPUS / "source_rights.jsonl"),
        _jsonl(CORPUS / "candidate_cards.jsonl"),
    )
    return tuple(item.validated for item in plan.cards[:3])


def test_batched_embeddings_are_cached(cards):
    calls: list[tuple[list[str], str]] = []

    def embedder(texts, *, input_type):
        calls.append((texts, input_type))
        return [[1.0, 0.0] for _ in texts]

    first = embed_cards_fail_soft(cards, embedder=embedder, expected_dimensions=2)
    second = embed_cards_fail_soft(cards, embedder=embedder, expected_dimensions=2)

    assert set(first.vectors) == {card.card_version_id for card in cards}
    assert not first.excluded
    assert second.cache_hits == 3
    assert len(calls) == 1
    assert calls[0][1] == "document"


def test_batch_failure_isolated_to_one_card_without_propagation(cards):
    bad_text = cards[1].claim

    def embedder(texts, *, input_type):
        if len(texts) > 1:
            raise RuntimeError("synthetic batch failure")
        if bad_text in texts[0]:
            raise RuntimeError("synthetic one-card failure")
        return [[0.0, 1.0]]

    result = embed_cards_fail_soft(cards, embedder=embedder, expected_dimensions=2)

    assert set(result.vectors) == {cards[0].card_version_id, cards[2].card_version_id}
    assert cards[1].card_version_id in result.excluded
    assert "synthetic one-card failure" in result.excluded[cards[1].card_version_id]


def test_invalid_vector_and_query_provider_failure_are_fail_soft(cards):
    result = embed_cards_fail_soft(
        cards[:1],
        embedder=lambda texts, *, input_type: [[1.0]],
        expected_dimensions=2,
    )
    query = embed_query_fail_soft(
        "objective",
        embedder=lambda texts, *, input_type: (_ for _ in ()).throw(RuntimeError("down")),
        expected_dimensions=2,
    )

    assert result.vectors == {}
    assert result.excluded[cards[0].card_version_id] == "invalid_embedding_dimensions"
    assert query is None
