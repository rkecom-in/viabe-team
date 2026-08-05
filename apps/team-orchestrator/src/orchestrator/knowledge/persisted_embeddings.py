"""VT-727 persisted embeddings for immutable GLOBAL knowledge-card versions.

The store is an optimization and restart-survival substrate, never an admission or effect gate.
Every vector is pinned to the model, dimensions, immutable card-version ID, and content digest.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from orchestrator.knowledge.contracts import KnowledgeCard
from orchestrator.knowledge.embeddings import EMBED_DIM, EMBED_MODEL, to_pgvector_literal


class EmbeddingStoreError(ValueError):
    """A vector cannot be bound safely to its immutable card version."""


class ConnectionLike(Protocol):
    def execute(self, query: str, params: object = ...) -> Any: ...


@dataclass(frozen=True)
class PersistedEmbedding:
    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    card_version_id: str
    model: str
    dimensions: int
    content_digest: str
    vector: tuple[float, ...]


def card_embedding_text(card: KnowledgeCard) -> str:
    return f"{card.claim}\n{card.distillation_note}"


def card_content_digest(card: KnowledgeCard) -> str:
    return hashlib.sha256(card_embedding_text(card).encode()).hexdigest()


def bind_embeddings(
    cards: Sequence[KnowledgeCard], vectors: Mapping[str, Sequence[float]]
) -> tuple[PersistedEmbedding, ...]:
    """Fail closed on missing, wrong-size, non-finite, or extra provider output."""

    card_by_id = {card.card_version_id: card for card in cards}
    if len(card_by_id) != len(cards):
        raise EmbeddingStoreError("card-version IDs must be unique")
    missing = set(card_by_id) - set(vectors)
    extra = set(vectors) - set(card_by_id)
    if missing or extra:
        raise EmbeddingStoreError(
            f"embedding/card ID mismatch: missing={len(missing)} extra={len(extra)}"
        )
    bound: list[PersistedEmbedding] = []
    for card_id in sorted(card_by_id):
        vector = tuple(float(value) for value in vectors[card_id])
        if len(vector) != EMBED_DIM or not all(math.isfinite(value) for value in vector):
            raise EmbeddingStoreError(f"{card_id}: invalid {EMBED_MODEL} vector")
        card = card_by_id[card_id]
        bound.append(
            PersistedEmbedding(
                card_version_id=card_id,
                model=EMBED_MODEL,
                dimensions=EMBED_DIM,
                content_digest=card_content_digest(card),
                vector=vector,
            )
        )
    return tuple(bound)


def persist_embeddings(conn: ConnectionLike, embeddings: Sequence[PersistedEmbedding]) -> None:
    """Insert only; conflicting immutable identity is left for the DB constraint to reject."""

    for item in embeddings:
        if item.model != EMBED_MODEL or item.dimensions != EMBED_DIM:
            raise EmbeddingStoreError("embedding model/dimension drift")
        conn.execute(
            "INSERT INTO public.knowledge_card_embeddings "
            "(card_id, embedding_model, embedding_dimensions, content_digest, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) ON CONFLICT (card_id) DO NOTHING",
            (
                item.card_version_id,
                item.model,
                item.dimensions,
                item.content_digest,
                to_pgvector_literal(list(item.vector)),
            ),
        )


__all__ = [
    "EmbeddingStoreError",
    "PersistedEmbedding",
    "bind_embeddings",
    "card_content_digest",
    "card_embedding_text",
    "persist_embeddings",
]
