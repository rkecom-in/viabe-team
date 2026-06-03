"""VT-70 — L4 skill-corpus retrieval.

``retrieve_documents`` embeds the query (voyage-4-lite) and runs a pgvector
cosine ANN search over ``l4_documents``, filtered by applicability
(business_type / city_tier; NULL on a doc = applies to all) and ranked by
similarity × priority. Global corpus (not tenant-scoped) → service-role read.
"""

from __future__ import annotations

from typing import Any, cast

from orchestrator.graph import get_pool
from orchestrator.knowledge.embeddings import embed_text, to_pgvector_literal
from orchestrator.knowledge.l4_types import L4Document

_MAX_TOP_K = 10

_SQL = """
    SELECT id, title, body, tags, applies_to_business_types,
           applies_to_city_tiers, priority, authored_by,
           (1 - (body_embedding <=> %(vec)s::vector)) AS similarity
      FROM l4_documents
     WHERE superseded_by IS NULL
       AND body_embedding IS NOT NULL
       AND (%(bt)s::text IS NULL OR applies_to_business_types IS NULL
            OR %(bt)s::text = ANY(applies_to_business_types))
       AND (%(tier)s::text IS NULL OR applies_to_city_tiers IS NULL
            OR %(tier)s::text = ANY(applies_to_city_tiers))
     -- Postgres can't use the SELECT alias inside an ORDER BY expression;
     -- repeat the cosine expression (the named param is reused, single bind).
     ORDER BY (1 - (body_embedding <=> %(vec)s::vector)) * priority DESC
     LIMIT %(k)s
"""


def retrieve_documents(
    query: str,
    *,
    business_type: str | None = None,
    city_tier: str | None = None,
    top_k: int = 5,
    query_embedding: list[float] | None = None,
) -> list[L4Document]:
    """Return the top-k applicable L4 docs for ``query`` (most relevant first).

    Embeds the query as a 'query'-type voyage vector, cosine-ranks against the
    corpus, applies the applicability filters, orders by similarity × priority.
    ``top_k`` clamped to [1, 10] (more bloats the agent's context). Returns []
    when the corpus is empty (no real docs loaded yet — VT-313).

    ``query_embedding`` (optional) lets a caller supply a precomputed vector
    (l1.py's pattern) — skips the embed call, e.g. to reuse one embedding across
    several lookups or to avoid a redundant billed call.
    """
    if not query or not query.strip():
        return []
    k = max(1, min(int(top_k), _MAX_TOP_K))
    vec = query_embedding if query_embedding is not None else embed_text(query, input_type="query")
    qvec = to_pgvector_literal(vec)

    with get_pool().connection() as conn:
        rows = conn.execute(
            _SQL, {"vec": qvec, "bt": business_type, "tier": city_tier, "k": k}
        ).fetchall()

    out: list[L4Document] = []
    for r in rows:
        d = cast("dict[str, Any]", dict(r))
        out.append(L4Document(
            id=d["id"], title=d["title"], body=d["body"], tags=d["tags"] or [],
            applies_to_business_types=d["applies_to_business_types"],
            applies_to_city_tiers=d["applies_to_city_tiers"],
            priority=d["priority"], authored_by=d["authored_by"],
            score=float(d["similarity"]) if d["similarity"] is not None else None,
        ))
    return out


__all__ = ["retrieve_documents"]
