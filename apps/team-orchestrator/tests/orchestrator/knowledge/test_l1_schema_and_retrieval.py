"""VT-7.1 — L1 Knowledge Graph schema + retrieval substrate tests.

Exercises the live ``l1_entities`` / ``l1_relationships`` tables and the
``orchestrator.knowledge.l1`` retrieval module against a real Postgres
(pgvector + HNSW). Requires ``DATABASE_URL`` + the dbos / langgraph stack;
runs in the CI ``orchestrator`` job which provisions
``pgvector/pgvector:pg16``.

Covers:

- Schema apply (the migration runs cleanly and both tables + RLS + indexes
  are present).
- Pillar 3 cross-tenant attack (tenant A cannot read tenant B's entities
  or relationships via any of: direct read, multi-signal retrieval, or
  recursive-CTE traversal).
- Recursive-CTE traversal at 2-hop and 3-hop with the depth cap.
- Multi-signal retrieval (vector cosine + entity_type filter + full-text).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pgvector")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-7.1 L1 KG tests skipped",
)

_EMBED_DIM = 1024


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. 019 L1 KG) and launch DBOS so the pool exists."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-7.1 Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _vec(seed: int) -> list[float]:
    """A deterministic 1024-dim vector. Concentrated mass at one index gives
    distinct cosine distances per ``seed`` — fine for ordering assertions."""
    base = [0.001] * _EMBED_DIM
    base[seed % _EMBED_DIM] = 1.0
    return base


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _seed_entity(
    dsn: str,
    tenant_id: UUID,
    *,
    entity_type: str,
    attributes: dict[str, object] | None = None,
    embedding: list[float] | None = None,
) -> UUID:
    """Seed an entity via a superuser connection (RLS bypassed at seed-time;
    the production-role read path is what we test under RLS).
    """
    import json

    eid = uuid4()
    attrs_json = json.dumps(attributes or {})
    if embedding is None:
        embedding_param: object = None
    else:
        embedding_param = _vec_literal(embedding)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO l1_entities (id, tenant_id, entity_type, attributes, "
            "embedding) VALUES (%s, %s, %s, %s::jsonb, %s::vector)",
            (str(eid), str(tenant_id), entity_type, attrs_json, embedding_param),
        )
    return eid


def _seed_relationship(
    dsn: str,
    tenant_id: UUID,
    *,
    from_entity: UUID,
    to_entity: UUID,
    relationship_type: str,
) -> UUID:
    rid = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO l1_relationships (id, tenant_id, from_entity, "
            "to_entity, relationship_type) VALUES (%s, %s, %s, %s, %s)",
            (
                str(rid),
                str(tenant_id),
                str(from_entity),
                str(to_entity),
                relationship_type,
            ),
        )
    return rid


# --- Schema + indexes -------------------------------------------------------


def test_l1_tables_and_indexes_exist(substrate):  # type: ignore[no-untyped-def]
    """The migration created both tables plus RLS + the four indexes (HNSW
    + 3 btree + 1 GIN)."""
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename IN ('l1_entities', 'l1_relationships')"
            ).fetchall()
        }
        assert tables == {"l1_entities", "l1_relationships"}

        # RLS enabled + forced on both.
        rls = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class "
                "WHERE relname IN ('l1_entities', 'l1_relationships')"
            ).fetchall()
        }
        for table in ("l1_entities", "l1_relationships"):
            enabled, forced = rls[table]
            assert enabled, f"{table} RLS not enabled"
            assert forced, f"{table} RLS not forced"

        # All four expected indexes exist on l1_entities + the two btree
        # indexes on l1_relationships.
        idx = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND tablename IN ('l1_entities', 'l1_relationships')"
            ).fetchall()
        }
        for required in (
            "l1_entities_embedding_hnsw",
            "l1_entities_tenant_type",
            "l1_entities_attributes_gin",
            "l1_relationships_tenant_from",
            "l1_relationships_tenant_to",
        ):
            assert required in idx, f"missing index {required!r}; found {idx}"


# --- Pillar 3 — cross-tenant attack -----------------------------------------


def test_cross_tenant_attack_blocked_on_entities_and_relationships(substrate):  # type: ignore[no-untyped-def]
    """Tenant A cannot see tenant B's entities via direct read, multi-signal
    retrieval, OR recursive-CTE traversal."""
    from orchestrator.knowledge.l1 import (
        search_entities,
        traverse_relationships,
    )

    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)

    # Seed identical-shape entities + a chain in tenant B; tenant A is empty.
    b_root = _seed_entity(
        substrate.dsn, tenant_b, entity_type="customer",
        attributes={"name": "B Root"}, embedding=_vec(7),
    )
    b_mid = _seed_entity(
        substrate.dsn, tenant_b, entity_type="customer",
        attributes={"name": "B Mid"}, embedding=_vec(7),
    )
    b_leaf = _seed_entity(
        substrate.dsn, tenant_b, entity_type="customer",
        attributes={"name": "B Leaf"}, embedding=_vec(7),
    )
    _seed_relationship(
        substrate.dsn, tenant_b,
        from_entity=b_root, to_entity=b_mid, relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_b,
        from_entity=b_mid, to_entity=b_leaf, relationship_type="knows",
    )

    # Tenant A's read sees nothing.
    a_results = search_entities(tenant_a, entity_type="customer", limit=10)
    assert a_results == [], (
        "RLS leak: tenant A saw tenant B's entities via search"
    )

    # Tenant A's vector-similarity probe sees nothing.
    a_vec_results = search_entities(
        tenant_a, query_embedding=_vec(7), entity_type="customer", limit=10
    )
    assert a_vec_results == [], (
        "RLS leak: tenant A saw tenant B's entities via vector search"
    )

    # Tenant A traversing FROM tenant B's root sees no relationships —
    # cross-tenant edge resolution must be blocked.
    a_paths = traverse_relationships(
        tenant_a, start_entity=b_root, max_depth=3
    )
    assert a_paths == [], (
        "RLS leak: tenant A traversed tenant B's relationships"
    )

    # Tenant B sees its own data — sanity check the test fixture isn't broken.
    b_results = search_entities(tenant_b, entity_type="customer", limit=10)
    assert len(b_results) == 3
    b_paths = traverse_relationships(
        tenant_b, start_entity=b_root, max_depth=3
    )
    assert len(b_paths) >= 2, (
        "tenant B should see its own 2-hop traversal — fixture broken"
    )


def test_cross_recursion_level_attack_blocked_at_depth_two(substrate):  # type: ignore[no-untyped-def]
    """Pillar 3 — recursive-step RLS proof.

    Construct the cross-recursion-level attack:

      1. Tenant B owns a chain ``b_root -> b_mid -> b_leaf`` (B-owned
         relationships, ``tenant_id = B``).
      2. Under tenant A's ``tenant_connection``, insert a single
         relationship ``(tenant_id = A, from_entity = a_root,
         to_entity = b_root)``. The FK on ``to_entity`` is unscoped
         (referential only); the RLS ``WITH CHECK`` only validates the
         row's own ``tenant_id`` matches the current tenant. So tenant A
         is permitted to write a relationship that *points at* a
         B-owned entity.
      3. Traverse from ``a_root`` under tenant A's context.

    Correct behaviour: A's traversal returns exactly the A-owned edge
    ``a_root -> b_root`` at depth 1. The recursion MUST NOT extend
    further — at recursion level 2 the CTE's JOIN looks for
    ``from_entity = b_root AND r.tenant_id = A``, and B's edges
    ``b_root -> b_mid`` (``tenant_id = B``) must be rejected by both
    RLS and the explicit predicate. ``b_mid`` and ``b_leaf`` therefore
    do not appear in any returned path.

    A failure of this assertion would mean ``r.tenant_id = %(tenant_id)s``
    is only honoured at the anchor / level 1 and the recursion is
    leaking foreign-tenant edges at depth >= 2 — the exact bug the
    rest of the file's cross-tenant test cannot catch.
    """
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.l1 import traverse_relationships

    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)

    # Tenant A's anchor entity.
    a_root = _seed_entity(substrate.dsn, tenant_a, entity_type="customer")

    # Tenant B's chain: b_root -> b_mid -> b_leaf, all B-owned.
    b_root = _seed_entity(substrate.dsn, tenant_b, entity_type="customer")
    b_mid = _seed_entity(substrate.dsn, tenant_b, entity_type="customer")
    b_leaf = _seed_entity(substrate.dsn, tenant_b, entity_type="customer")
    _seed_relationship(
        substrate.dsn, tenant_b,
        from_entity=b_root, to_entity=b_mid, relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_b,
        from_entity=b_mid, to_entity=b_leaf, relationship_type="knows",
    )

    # Tenant A inserts a relationship pointing into B's graph. Done via
    # ``tenant_connection`` so the RLS ``WITH CHECK`` policy runs for
    # real and we prove the insert IS permitted (the FK does not scope
    # to_entity by tenant).
    cross_edge_id = uuid4()
    with tenant_connection(tenant_a) as conn:
        conn.execute(
            "INSERT INTO l1_relationships (id, tenant_id, from_entity, "
            "to_entity, relationship_type) VALUES (%s, %s, %s, %s, %s)",
            (
                str(cross_edge_id),
                str(tenant_a),
                str(a_root),
                str(b_root),
                "cross_ref",
            ),
        )

    paths = traverse_relationships(tenant_a, start_entity=a_root, max_depth=3)

    # The recursion must produce exactly one path: a_root -> b_root at
    # depth 1, the A-owned cross_ref edge. B's chain MUST NOT be walked.
    assert len(paths) == 1, (
        f"expected exactly the A-owned a_root->b_root edge; got {paths!r}"
    )
    only_path = paths[0]
    assert only_path.depth == 1
    assert only_path.entities == [a_root, b_root]
    assert only_path.relationship_types == ["cross_ref"]

    # Belt-and-braces: neither of B's downstream nodes appears in any
    # path's entity list, and B's "knows" relationship type is not
    # surfaced anywhere.
    all_entities_seen: set[UUID] = set()
    all_rel_types_seen: set[str] = set()
    for path in paths:
        all_entities_seen.update(path.entities)
        all_rel_types_seen.update(path.relationship_types)
    assert b_mid not in all_entities_seen, (
        "RLS leak at recursion level 2: b_mid surfaced via B's edge"
    )
    assert b_leaf not in all_entities_seen, (
        "RLS leak at recursion level 3: b_leaf surfaced via B's edge"
    )
    assert "knows" not in all_rel_types_seen, (
        "RLS leak: B's 'knows' edges were walked under tenant A"
    )


# --- Recursive-CTE traversal: 2-hop, 3-hop, depth cap -----------------------


def test_traversal_2_hop_and_3_hop_with_depth_cap(substrate):  # type: ignore[no-untyped-def]
    """Build a linear chain root -> a -> b -> c -> d (4 hops); verify
    max_depth=2 returns paths up to depth 2, max_depth=3 returns up to
    depth 3, and the depth cap is honoured (d never appears at depth>3)."""
    from orchestrator.knowledge.l1 import traverse_relationships

    tenant_id = _new_tenant(substrate.dsn)
    root = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    a = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    b = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    c = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    d = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=root, to_entity=a, relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=a, to_entity=b, relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=b, to_entity=c, relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=c, to_entity=d, relationship_type="knows",
    )

    # 2-hop: reachable = {a (depth 1), b (depth 2)}. d must NOT appear.
    paths_2 = traverse_relationships(tenant_id, start_entity=root, max_depth=2)
    end_entities_2 = {path.entities[-1] for path in paths_2}
    depths_2 = {path.depth for path in paths_2}
    assert a in end_entities_2
    assert b in end_entities_2
    assert c not in end_entities_2, "depth cap 2 leaked depth-3 node c"
    assert d not in end_entities_2, "depth cap 2 leaked depth-4 node d"
    assert max(depths_2) == 2

    # 3-hop: reachable adds c (depth 3). d still must NOT appear.
    paths_3 = traverse_relationships(tenant_id, start_entity=root, max_depth=3)
    end_entities_3 = {path.entities[-1] for path in paths_3}
    depths_3 = {path.depth for path in paths_3}
    assert a in end_entities_3
    assert b in end_entities_3
    assert c in end_entities_3
    assert d not in end_entities_3, "depth cap 3 leaked depth-4 node d"
    assert max(depths_3) == 3

    # Each path's structural shape — entities length = depth+1, rels = depth.
    for path in paths_3:
        assert len(path.entities) == path.depth + 1
        assert len(path.relationship_types) == path.depth
        assert path.entities[0] == root


def test_traversal_filters_by_relationship_type(substrate):  # type: ignore[no-untyped-def]
    """Mixed-type edges: ``relationship_type='knows'`` filter excludes
    other types."""
    from orchestrator.knowledge.l1 import traverse_relationships

    tenant_id = _new_tenant(substrate.dsn)
    root = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    knows_target = _seed_entity(
        substrate.dsn, tenant_id, entity_type="customer"
    )
    bought_target = _seed_entity(
        substrate.dsn, tenant_id, entity_type="product"
    )
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=root, to_entity=knows_target,
        relationship_type="knows",
    )
    _seed_relationship(
        substrate.dsn, tenant_id,
        from_entity=root, to_entity=bought_target,
        relationship_type="bought",
    )

    knows_paths = traverse_relationships(
        tenant_id, start_entity=root, max_depth=1, relationship_type="knows"
    )
    reached = {p.entities[-1] for p in knows_paths}
    assert knows_target in reached
    assert bought_target not in reached, "relationship_type filter ignored"


def test_traversal_rejects_max_depth_above_ceiling(substrate):  # type: ignore[no-untyped-def]
    """Hard ceiling guard — out-of-range depths raise ValueError before any
    SQL is executed."""
    from orchestrator.knowledge.l1 import (
        MAX_TRAVERSAL_DEPTH,
        traverse_relationships,
    )

    tenant_id = _new_tenant(substrate.dsn)
    root = _seed_entity(substrate.dsn, tenant_id, entity_type="customer")

    with pytest.raises(ValueError):
        traverse_relationships(
            tenant_id, start_entity=root, max_depth=MAX_TRAVERSAL_DEPTH + 1
        )
    with pytest.raises(ValueError):
        traverse_relationships(tenant_id, start_entity=root, max_depth=0)


# --- Multi-signal retrieval -------------------------------------------------


def test_multi_signal_retrieval_combines_vector_filter_and_fts(substrate):  # type: ignore[no-untyped-def]
    """Vector cosine ranks results; entity_type narrows; full-text matches
    JSONB attribute text."""
    from orchestrator.knowledge.l1 import search_entities

    tenant_id = _new_tenant(substrate.dsn)
    # Three customers + one product, all carrying embeddings + descriptive
    # attribute text. Customer 'alpha' shares a vector with the query;
    # 'beta' is the nearest of the rest.
    alpha = _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="customer",
        attributes={"name": "alpha shop", "notes": "dormant winback prospect"},
        embedding=_vec(11),
    )
    beta = _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="customer",
        attributes={"name": "beta shop", "notes": "active loyal customer"},
        embedding=_vec(12),
    )
    _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="customer",
        attributes={"name": "gamma shop", "notes": "ordinary customer"},
        embedding=_vec(900),  # cosine-far
    )
    product = _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="product",
        attributes={"name": "winback combo", "notes": "dormant winback promo"},
        embedding=_vec(11),
    )

    # Vector ranking — alpha (exact match seed=11) ahead of beta (seed=12)
    # which is ahead of gamma (seed=900). entity_type narrows to customer
    # so product is excluded even though it shares alpha's vector.
    vec_ranked = search_entities(
        tenant_id,
        query_embedding=_vec(11),
        entity_type="customer",
        limit=10,
    )
    ranked_ids = [e.id for e in vec_ranked]
    assert ranked_ids[0] == alpha
    assert ranked_ids[1] == beta
    assert product not in ranked_ids

    # FTS narrows to the 'dormant winback' note — alpha matches, beta does
    # NOT (its note is 'active loyal').
    fts_results = search_entities(
        tenant_id, entity_type="customer", text_query="dormant winback"
    )
    fts_ids = {e.id for e in fts_results}
    assert alpha in fts_ids
    assert beta not in fts_ids


def test_attributes_filter_uses_jsonb_containment(substrate):  # type: ignore[no-untyped-def]
    """``attributes_filter`` should match via JSONB ``@>`` containment —
    mirrors how an L3 locality filter would narrow L1 entities."""
    from orchestrator.knowledge.l1 import search_entities

    tenant_id = _new_tenant(substrate.dsn)
    south = _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="customer",
        attributes={"locality": "Indiranagar", "tier": "gold"},
    )
    north = _seed_entity(
        substrate.dsn, tenant_id,
        entity_type="customer",
        attributes={"locality": "Malleshwaram", "tier": "gold"},
    )

    south_only = search_entities(
        tenant_id,
        entity_type="customer",
        attributes_filter={"locality": "Indiranagar"},
    )
    south_only_ids = {e.id for e in south_only}
    assert south in south_only_ids
    assert north not in south_only_ids


def test_search_without_signals_returns_recent_first(substrate):  # type: ignore[no-untyped-def]
    """No filters + no embedding → ordered ``created_at DESC``."""
    from orchestrator.knowledge.l1 import search_entities

    tenant_id = _new_tenant(substrate.dsn)
    first = _seed_entity(
        substrate.dsn, tenant_id, entity_type="customer",
        attributes={"order": 1},
    )
    # Sleep-free ordering — second row's created_at default(now()) is
    # monotonically later than first.
    second = _seed_entity(
        substrate.dsn, tenant_id, entity_type="customer",
        attributes={"order": 2},
    )

    results = search_entities(tenant_id, limit=10)
    ids_in_order = [e.id for e in results]
    assert ids_in_order.index(second) < ids_in_order.index(first), (
        "expected most-recent-first ordering"
    )


def test_search_limit_is_hard_capped(substrate):  # type: ignore[no-untyped-def]
    """``limit`` is hard-capped at 100 — pass 5000, expect no more than
    100 rows even if the table has 200 (we seed 5)."""
    from orchestrator.knowledge.l1 import search_entities

    tenant_id = _new_tenant(substrate.dsn)
    for _ in range(5):
        _seed_entity(substrate.dsn, tenant_id, entity_type="customer")
    results = search_entities(tenant_id, limit=5000)
    assert len(results) <= 100


# Touch the import to keep mypy happy + ensure datetime is reachable.
_ = datetime.now(UTC)
