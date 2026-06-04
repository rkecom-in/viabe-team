"""VT-306 — typed-wrapper RLS canary (the load-bearing cross-tenant negative).

Real PG (mock cursors hide RLS): the wrappers run under tenant_connection
(SET ROLE app_role + GUC) + assert_tenant_scoped. Proves: (1) a write+read
round-trips for its own tenant; (2) tenant A's wrapper CANNOT see tenant B's row
(find_by_id -> None, list_for_tenant excludes it) — the invariant the whole
migration exists to guarantee; (3) insert FORCES tenant_id to the scoped tenant
(a payload tenant_id is ignored). CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-306 wrapper RLS canary skipped",
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
    # Point the global pool at the test dsn so tenant_connection (and thus the
    # wrappers) work without launching the full DBOS substrate.
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


def test_wrapper_round_trip_and_cross_tenant_denial(pool):
    from orchestrator._tenant_guard import TenantIsolationError
    from orchestrator.db.wrappers import CustomersWrapper

    cw = CustomersWrapper()
    tid_a = _tenant(pool, "wrap-A")
    tid_b = _tenant(pool, "wrap-B")

    # (1) round-trip: insert under A, read it back under A.
    row = cw.insert(tid_a, {"display_name": "Asha", "phone_e164": "+919900000001"})
    cust_a = str(row["id"])
    assert cw.find_by_id(tid_a, cust_a) is not None

    # insert under B (its own customer).
    row_b = cw.insert(tid_b, {"display_name": "Bhavna", "phone_e164": "+919900000002"})
    cust_b = str(row_b["id"])

    # (2) CROSS-TENANT NEGATIVE — A's wrapper cannot see B's customer.
    assert cw.find_by_id(tid_a, cust_b) is None, "RLS must hide B's row from A"
    a_ids = {str(r["id"]) for r in cw.list_for_tenant(tid_a)}
    assert cust_a in a_ids
    assert cust_b not in a_ids, "list_for_tenant(A) leaked B's row"

    # (3) insert FORCES tenant_id — a payload tenant_id pointing at B is ignored;
    #     the row is A's. (If it were honoured, assert_tenant_scoped would raise.)
    forced = cw.insert(tid_a, {"tenant_id": tid_b, "display_name": "X"})
    assert str(forced["tenant_id"]) == tid_a, "insert must force the scoped tenant_id"

    # (4) a tenant_id mismatch is caught by assert_tenant_scoped (belt over RLS):
    #     reading B's customer while scoped to A (bypassing find_by_id's WHERE)
    #     would breach — exercise the guard directly.
    with pytest.raises(TenantIsolationError):
        from orchestrator._tenant_guard import assert_tenant_scoped
        from uuid import UUID
        assert_tenant_scoped([{"tenant_id": UUID(tid_b)}], UUID(tid_a))
