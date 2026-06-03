"""VT-70 — L4 skill-corpus canary (live PG).

Two layers:
  * The retrieval LOGIC (ANN ranking, applicability filter, Composer wire) is
    proven with DIRECTLY-INSERTED synthetic 1024-dim embeddings — deterministic,
    no Voyage call, always runs.
  * The real Voyage embedding path (DR-15 fail-not-skip) is one test gated on
    VOYAGE_API_KEY (the pre-push hook sources voyage.env). NOTE: the dev voyage
    key is free-tier (3 RPM); reliable real-call + production L4 need a paid key
    (flagged to Cowork). The synthetic-embedding layer keeps the canary robust
    regardless of that account limit.

CL-422 synthetic placeholder corpus (NOT the real VT-313 docs).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — L4 corpus tests skipped",
)


def _synthetic_vec(idx: int) -> list[float]:
    """A deterministic near-one-hot 1024-dim unit vector (1.0 at idx, else 0).
    Cosine similarity = 1 for the matching query, 0 otherwise → deterministic ANN."""
    v = [0.0] * 1024
    v[idx] = 1.0
    return v


# (title, applies_to_business_types, embedding index)
_DOCS = [
    ("Cafe dormant re-engagement", ["cafe"], 0),
    ("Retail discount discipline", ["retail"], 1),
    ("Services vertical cadence", ["services"], 2),
    ("Festival timing for outreach", None, 3),       # applies to all
    ("When NOT to campaign", None, 4),               # applies to all
]


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


@pytest.fixture(scope="module")
def seeded(pool):
    """Insert the synthetic corpus directly (crafted embeddings, no Voyage call)."""
    from orchestrator.knowledge.embeddings import to_pgvector_literal

    with pool.connection() as conn, conn.transaction():
        for title, bts, idx in _DOCS:
            conn.execute(
                "INSERT INTO l4_documents "
                "(title, body, body_embedding, applies_to_business_types, authored_by) "
                "VALUES (%s, %s, %s::vector, %s, 'synthetic') "
                "ON CONFLICT (title, version) DO NOTHING",
                (title, f"body of {title} — domain knowledge.",
                 to_pgvector_literal(_synthetic_vec(idx)), bts),
            )
    return _DOCS


# --- retrieval LOGIC (synthetic embeddings — deterministic, no Voyage) -------


def test_ann_ranks_nearest_doc_first(pool, seeded):
    from orchestrator.knowledge.l4_query import retrieve_documents

    # Query vector identical to the cafe doc's → it must rank first (cosine=1).
    docs = retrieve_documents(
        "cafe re-engagement", top_k=5, query_embedding=_synthetic_vec(0),
    )
    assert docs and docs[0].title == "Cafe dormant re-engagement"


def test_applicability_filter_excludes_non_matching(pool, seeded):
    from orchestrator.knowledge.l4_query import retrieve_documents

    docs = retrieve_documents(
        "discounting", business_type="cafe", top_k=10,
        query_embedding=_synthetic_vec(0),
    )
    titles = {d.title for d in docs}
    assert "Cafe dormant re-engagement" in titles       # cafe-applicable
    assert "Festival timing for outreach" in titles     # applies-to-all
    assert "Retail discount discipline" not in titles   # retail-only → excluded
    assert "Services vertical cadence" not in titles    # services-only → excluded


def test_composer_reflects_skills(pool, seeded, monkeypatch):
    from orchestrator.context_builder import _build_l4_skills

    # No Voyage call: patch the embed at l4_query's call site to the cafe vector.
    monkeypatch.setattr(
        "orchestrator.knowledge.l4_query.embed_text",
        lambda *a, **k: _synthetic_vec(0),
    )
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, business_type, city_tier) "
            "VALUES (%s, 'l4 cafe', 'founding', 'paid_active', 'cafe', 'tier_2')",
            (tid,),
        )
    skills, ok = _build_l4_skills(UUID(tid), "re-engage dormant cafe customers")
    assert ok is True
    assert skills.available is True
    assert any(s["title"] == "Cafe dormant re-engagement" for s in skills.skills)
    assert all(len(s["excerpt"]) <= 300 for s in skills.skills)  # excerpts; full via MCP tool


# --- real Voyage embedding path (DR-15 fail-not-skip) ------------------------


@pytest.mark.skipif(
    not os.environ.get("VOYAGE_API_KEY"),
    reason="VOYAGE_API_KEY not set — real-voyage-call canary needs voyage.env "
    "(sourced by the pre-push hook). Dev key is free-tier 3 RPM; paid key needed "
    "for prod L4 + reliable CI.",
)
def test_real_voyage_embedding_is_1024_dim():
    """DR-15 real billed call: voyage-4-lite returns exactly a 1024-dim vector
    (with bounded rate-limit backoff for the free-tier dev key)."""
    from orchestrator.knowledge.embeddings import EMBED_DIM, embed_text

    vec = embed_text("cafe re-engagement probe", input_type="query")
    assert len(vec) == EMBED_DIM == 1024
    assert all(isinstance(x, float) for x in vec[:5])
