"""Fail-soft, process-local O8 embeddings for VT-726 shadow retrieval.

Vectors are deliberately not persisted in this checkpoint. Successful document embeddings are
cached by immutable card-version ID plus content hash. A failed card is excluded; it never fails
an owner turn. The dev canary may still fail loudly when *all* cards fail because that is a proof
gate, not the runtime behavior.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from orchestrator.knowledge.contracts import KnowledgeCard
from orchestrator.knowledge.embeddings import EMBED_DIM, embed_redacted_texts

Embedder = Callable[..., list[list[float]]]
_CACHE: dict[tuple[str, str], tuple[float, ...]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ShadowEmbeddingResult:
    """Advisory retrieval substrate; it cannot grant permission for an effect."""

    AUTHORIZES_EFFECTS: ClassVar[bool] = False
    vectors: dict[str, tuple[float, ...]]
    excluded: dict[str, str]
    cache_hits: int


def embed_cards_fail_soft(
    cards: Sequence[KnowledgeCard],
    *,
    embedder: Embedder = embed_redacted_texts,
    batch_size: int = 32,
    expected_dimensions: int = EMBED_DIM,
) -> ShadowEmbeddingResult:
    """Batch and cache card embeddings, isolating provider or payload failures per card."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    vectors: dict[str, tuple[float, ...]] = {}
    excluded: dict[str, str] = {}
    pending: list[tuple[KnowledgeCard, str, tuple[str, str]]] = []
    cache_hits = 0
    for card in cards:
        text = f"{card.claim}\n{card.distillation_note}"
        key = (card.card_version_id, hashlib.sha256(text.encode()).hexdigest())
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            vectors[card.card_version_id] = cached
            cache_hits += 1
        else:
            pending.append((card, text, key))

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        try:
            embedded = embedder([text for _, text, _ in batch], input_type="document")
            if len(embedded) != len(batch):
                raise ValueError("embedder returned a different number of vectors")
            for (card, _, key), vector in zip(batch, embedded, strict=True):
                _accept_vector(
                    card, key, vector, vectors, excluded, expected_dimensions=expected_dimensions
                )
        except Exception as batch_error:  # fail-soft boundary; isolate the failing payload next
            for card, text, key in batch:
                try:
                    embedded = embedder([text], input_type="document")
                    if len(embedded) != 1:
                        raise ValueError("embedder did not return one vector")
                    _accept_vector(
                        card,
                        key,
                        embedded[0],
                        vectors,
                        excluded,
                        expected_dimensions=expected_dimensions,
                    )
                except Exception as card_error:  # a card failure is exclusion, never propagation
                    excluded[card.card_version_id] = (
                        f"{type(card_error).__name__}: {card_error}"[:300]
                    )
            if not batch:
                excluded["batch"] = f"{type(batch_error).__name__}: {batch_error}"[:300]
    return ShadowEmbeddingResult(vectors=vectors, excluded=excluded, cache_hits=cache_hits)


def embed_query_fail_soft(
    text: str,
    *,
    embedder: Embedder = embed_redacted_texts,
    expected_dimensions: int = EMBED_DIM,
) -> tuple[float, ...] | None:
    """Return a safe query vector or ``None`` without propagating provider failure."""

    try:
        embedded = embedder([text], input_type="query")
        if len(embedded) != 1:
            return None
        vector = tuple(float(value) for value in embedded[0])
        if len(vector) != expected_dimensions or not all(math.isfinite(value) for value in vector):
            return None
        return vector
    except Exception:  # same intentional runtime fail-soft boundary as card embedding
        return None


def clear_shadow_embedding_cache() -> None:
    """Test helper; VT-726 has no cross-process or persistent cache."""

    with _CACHE_LOCK:
        _CACHE.clear()


def _accept_vector(
    card: KnowledgeCard,
    key: tuple[str, str],
    vector: Sequence[float],
    vectors: dict[str, tuple[float, ...]],
    excluded: dict[str, str],
    *,
    expected_dimensions: int,
) -> None:
    normalized = tuple(float(value) for value in vector)
    if len(normalized) != expected_dimensions:
        excluded[card.card_version_id] = "invalid_embedding_dimensions"
        return
    if not all(math.isfinite(value) for value in normalized):
        excluded[card.card_version_id] = "non_finite_embedding"
        return
    with _CACHE_LOCK:
        _CACHE[key] = normalized
    vectors[card.card_version_id] = normalized


__all__ = [
    "ShadowEmbeddingResult",
    "clear_shadow_embedding_cache",
    "embed_cards_fail_soft",
    "embed_query_fail_soft",
]
