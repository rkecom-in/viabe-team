"""VT-72 — typed tenant-scoped wrapper substrate tests.

Live Postgres via DATABASE_URL (CI orchestrator job). Exercises the wrapper
CRUD + cross-tenant isolation + the layer-2 validation primitive + the lint.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — wrapper tests skipped",
)


@pytest.fixture(scope="module")
def db():
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


def _tenant(db) -> str:
    tid = str(uuid4())
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt72-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def test_insert_forces_tenant_and_roundtrips(db):
    from orchestrator.db.wrappers import CustomersWrapper

    tid = _tenant(db)
    w = CustomersWrapper()
    # tenant_id in payload is ignored — wrapper forces the scoped tenant.
    row = w.insert(tid, {"display_name": "Asha", "tenant_id": str(uuid4())})
    assert str(row["tenant_id"]) == tid
    fetched = w.find_by_id(tid, row["id"])
    assert fetched is not None
    assert fetched["display_name"] == "Asha"
    listed = w.list_for_tenant(tid)
    assert any(str(r["id"]) == str(row["id"]) for r in listed)


def test_cross_tenant_isolation(db):
    from orchestrator.db.wrappers import CustomersWrapper

    tid_a = _tenant(db)
    tid_b = _tenant(db)
    w = CustomersWrapper()
    row_a = w.insert(tid_a, {"display_name": "A-only"})
    # Tenant B cannot see A's row (RLS hides it → empty, not a leak).
    assert w.find_by_id(tid_b, row_a["id"]) is None
    assert all(str(r["id"]) != str(row_a["id"]) for r in w.list_for_tenant(tid_b))


def test_validation_primitive_raises_on_mismatch(db):
    """Layer-2 enforcement: a row whose tenant_id != input raises (defence in
    depth, even if RLS were somehow bypassed)."""
    from orchestrator._tenant_guard import TenantIsolationError
    from orchestrator.db.wrappers import CustomersWrapper

    w = CustomersWrapper()
    tid = uuid4()
    with pytest.raises(TenantIsolationError):
        w._validate([{"tenant_id": uuid4(), "id": uuid4()}], tid)


def test_delete_is_tenant_scoped(db):
    from orchestrator.db.wrappers import CustomersWrapper

    tid_a = _tenant(db)
    tid_b = _tenant(db)
    w = CustomersWrapper()
    row = w.insert(tid_a, {"display_name": "del-me"})
    # B cannot delete A's row.
    assert w.delete(tid_b, row["id"]) == 0
    # A can.
    assert w.delete(tid_a, row["id"]) == 1
    assert w.find_by_id(tid_a, row["id"]) is None


def test_no_direct_access_lint_passes():
    """The Phase-1 lint must pass on the current tree (allowlist complete)."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[5]
    result = subprocess.run(
        [sys.executable, "scripts/check_no_direct_tenant_db_access.py"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
