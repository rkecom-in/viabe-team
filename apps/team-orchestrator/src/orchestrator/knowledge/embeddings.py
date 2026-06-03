"""VT-70 — production text-embedding helper (voyage-4-lite, 1024-dim).

The first production embedding call. Until now embeddings were caller-supplied
(l1.py takes a query_embedding) and the voyage SDK was exercised only in a test.
L4 retrieval must embed the query to be plugged-in, so this is the shared seam.

Model + dimension are pinned to ground truth (mig 019 L1 = vector(1024); VT-7.1
verified voyage-4-lite returns 1024). DR-15: this makes a REAL billed Voyage
call — ``embed_text`` RAISES (never silently returns a zero/None vector) when
``VOYAGE_API_KEY`` is absent, so a canary fails-not-skips where the key is wired
(the pre-push hook sources .viabe/secrets/voyage.env when present).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Literal

logger = logging.getLogger(__name__)

EMBED_MODEL = "voyage-4-lite"
EMBED_DIM = 1024

# Bounded retry on transient rate-limits. The DEV voyage key is free-tier
# (3 RPM / 10K TPM, no payment method — flagged for a paid key before prod L4,
# which embeds per dispatch). A paid key rarely hits this; the retry keeps a
# burst (e.g. the canary's multiple calls) from failing spuriously. Callers that
# can't tolerate the wait (the live Composer) already treat L4 as best-effort.
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF_S = 21.0  # just over the 60s/3 = 20s free-tier window

InputType = Literal["query", "document"]


class EmbeddingKeyMissingError(RuntimeError):
    """VOYAGE_API_KEY absent — embedding cannot run. Raised (not skipped) so the
    failure is loud at the call site (DR-15 fail-not-skip)."""


def embed_texts(
    texts: list[str], *, input_type: InputType | None = None
) -> list[list[float]]:
    """Embed a batch of texts → list of 1024-dim vectors (voyage-4-lite).

    ``input_type`` ('query' | 'document') lets Voyage optimise the embedding for
    retrieval (asymmetric search): the corpus is embedded as 'document', the
    query as 'query'. Raises ``EmbeddingKeyMissingError`` when the key is absent.
    """
    if not texts:
        return []
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise EmbeddingKeyMissingError(
            "VOYAGE_API_KEY not set — cannot embed (DR-15: real call required, "
            "no silent fallback). Source .viabe/secrets/voyage.env."
        )
    import voyageai  # lazy — keeps the SDK off non-embedding import paths
    from voyageai import error as voyage_error

    client = voyageai.Client(api_key=key)
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        try:
            resp = client.embed(list(texts), model=EMBED_MODEL, input_type=input_type)
            return [list(vec) for vec in resp.embeddings]
        except voyage_error.RateLimitError:
            if attempt >= _RATE_LIMIT_RETRIES:
                raise
            logger.warning(
                "voyage rate-limited (attempt %d/%d) — backing off %.0fs "
                "(free-tier key? a paid voyage key is needed for prod L4 throughput)",
                attempt + 1, _RATE_LIMIT_RETRIES, _RATE_LIMIT_BACKOFF_S,
            )
            time.sleep(_RATE_LIMIT_BACKOFF_S)
    raise RuntimeError("unreachable")  # loop returns or raises


def embed_text(text: str, *, input_type: InputType | None = None) -> list[float]:
    """Embed one text → a single 1024-dim vector."""
    return embed_texts([text], input_type=input_type)[0]


def to_pgvector_literal(vec: list[float]) -> str:
    """Format a float vector as pgvector's text literal ``[v1,v2,...]`` for an
    ``::vector`` cast — mirrors l1.py's approach (avoids per-conn register_vector
    on the shared pool)."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


__all__ = [
    "EMBED_DIM", "EMBED_MODEL", "EmbeddingKeyMissingError",
    "embed_text", "embed_texts", "to_pgvector_literal",
]
