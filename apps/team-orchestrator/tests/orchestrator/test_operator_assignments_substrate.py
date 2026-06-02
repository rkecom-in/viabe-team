"""VT-290 — operator_assignments substrate (Rule #15 canary, real Postgres).

The VTR↔business scoping table (migration 072). Deny-all FORCE RLS: only the service role
(superuser / pool, RLS-bypassing) touches it; a tenant-scoped app_role connection sees
NOTHING. The active-assignment scoping query (what team-web's resolveAssignedTenants runs)
returns only un-revoked assignments. CL-422 synthetic.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-290 operator_assignments tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-290 test', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _seed(dsn: str, operator_id: str, tenant_id: str, *, revoked: bool = False) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO operator_assignments (operator_id, tenant_id, unassigned_at) "
            "VALUES (%s, %s, %s)",
            (operator_id, tenant_id, "now()" if revoked else None),
        )


def test_active_scoping_returns_only_unrevoked(substrate):
    op = str(uuid4())
    t_a, t_b, t_c = _tenant(substrate.dsn), _tenant(substrate.dsn), _tenant(substrate.dsn)
    _seed(substrate.dsn, op, t_a)
    _seed(substrate.dsn, op, t_b)
    _seed(substrate.dsn, op, t_c, revoked=True)  # revoked → excluded
    # the exact query team-web's resolveAssignedTenants runs (service-role / superuser).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tenant_id FROM operator_assignments "
            "WHERE operator_id = %s AND unassigned_at IS NULL",
            (op,),
        ).fetchall()
    got = {str(r[0]) for r in rows}
    assert got == {t_a, t_b}            # t_c (revoked) excluded — reassignment semantics


def test_reassign_rescopes(substrate):
    op1, op2 = str(uuid4()), str(uuid4())
    t = _tenant(substrate.dsn)
    _seed(substrate.dsn, op1, t)
    # reassign: revoke op1, grant op2
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE operator_assignments SET unassigned_at = now() "
            "WHERE operator_id = %s AND tenant_id = %s",
            (op1, t),
        )
    _seed(substrate.dsn, op2, t)

    def _active(op: str) -> set[str]:
        with psycopg.connect(substrate.dsn, autocommit=True) as c:
            rows = c.execute(
                "SELECT tenant_id FROM operator_assignments "
                "WHERE operator_id = %s AND unassigned_at IS NULL",
                (op,),
            ).fetchall()
        return {str(r[0]) for r in rows}

    assert _active(op1) == set()        # op1 no longer sees t
    assert _active(op2) == {t}          # op2 now does


def test_deny_all_rls_blocks_app_role(substrate):
    """Deny-all FORCE RLS: a tenant-scoped app_role connection sees ZERO rows."""
    from orchestrator.db import tenant_connection

    op = str(uuid4())
    t = _tenant(substrate.dsn)
    _seed(substrate.dsn, op, t)
    with tenant_connection(t) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM operator_assignments")
        row = cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 0                       # service-role only; app_role denied


def test_active_unique_allows_reassign_after_revoke(substrate):
    """Partial-unique (active only) lets the same (op,tenant) be re-granted after revoke."""
    op = str(uuid4())
    t = _tenant(substrate.dsn)
    _seed(substrate.dsn, op, t)
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE operator_assignments SET unassigned_at = now() "
            "WHERE operator_id = %s AND tenant_id = %s",
            (op, t),
        )
    _seed(substrate.dsn, op, t)         # re-grant — must NOT violate the partial unique
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM operator_assignments "
            "WHERE operator_id = %s AND tenant_id = %s AND unassigned_at IS NULL",
            (op, t),
        ).fetchone()[0]
    assert n == 1
