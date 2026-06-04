"""VT-325 — platform_listings source canary (Rule #15, real PG, CL-422 synthetic).

`write_platform_listing` upserts the listing row + emits `platform_listing_updated`
to the VT-65 outbox atomically, then drains → the existing
`_h_platform_listing_updated` consumer projects a PLATFORM_LISTING node + a
HAS_LISTING edge. Plus the cross-tenant negative (tenant A cannot see B's listing
through the wrapper). Mock cursors hide RLS, so this runs on a live DB.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-325 platform_listings canary skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


def _tenant(pool, name: str) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'founding', 'paid_active')", (tid, name),
        )
    return tid


def _scoped_count(pool, tenant_id: str, sql: str) -> int:
    with pool.connection() as c:
        c.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))
        c.execute("SET ROLE app_role")
        try:
            return int(c.execute(sql, (tenant_id,)).fetchone()["n"])
        finally:
            c.execute("RESET ROLE")


def test_write_listing_projects_to_kg_and_isolates(pool):
    from orchestrator.db.wrappers import PlatformListingsWrapper
    from orchestrator.integrations.platform_listings import write_platform_listing

    a = _tenant(pool, "pl-A")
    row = write_platform_listing(
        a, "swiggy", "rest-1", rating=4.2,
        attributes={"cuisines": ["South Indian"], "category": "restaurant"},
    )
    assert row["platform"] == "swiggy"
    assert float(row["rating"]) == 4.2

    # The emit→drain chain projected the listing into the KG (the dormant consumer).
    n_ent = _scoped_count(
        pool, a,
        "SELECT count(*) AS n FROM l1_entities "
        "WHERE tenant_id = %s AND entity_type = 'platform_listing'",
    )
    n_rel = _scoped_count(
        pool, a,
        "SELECT count(*) AS n FROM l1_relationships "
        "WHERE tenant_id = %s AND relationship_type = 'has_listing'",
    )
    assert n_ent >= 1, "platform_listing node not projected by the consumer"
    assert n_rel >= 1, "has_listing edge not projected by the consumer"

    # CROSS-TENANT NEGATIVE — A cannot see B's listing through the wrapper.
    b = _tenant(pool, "pl-B")
    rb = write_platform_listing(b, "swiggy", "rest-1", rating=3.0)
    w = PlatformListingsWrapper()
    assert w.find_by_id(a, str(rb["id"])) is None, "RLS must hide B's listing from A"
    a_ids = {str(r["id"]) for r in w.list_for_tenant(a)}
    assert str(row["id"]) in a_ids
    assert str(rb["id"]) not in a_ids, "list_for_tenant(A) leaked B's listing"


def test_reupsert_same_listing_is_idempotent_one_row(pool):
    from orchestrator.db.wrappers import PlatformListingsWrapper
    from orchestrator.integrations.platform_listings import write_platform_listing

    t = _tenant(pool, "pl-idem")
    r1 = write_platform_listing(t, "zomato", "z-9", rating=3.5)
    r2 = write_platform_listing(t, "zomato", "z-9", rating=4.9)
    assert str(r1["id"]) == str(r2["id"])  # ON CONFLICT updated in place
    assert float(r2["rating"]) == 4.9
    listings = [r for r in PlatformListingsWrapper().list_for_tenant(t)
                if r["platform"] == "zomato"]
    assert len(listings) == 1
