"""VT-530 (C2a) — the recovery_attempted audit event (live Postgres).

Proves the manager's implicit self-handling (the VT-484 tool-error → tool_result seam) becomes
VISIBLE in the audit spine: a ``recovery_attempted`` row lands, scoped to the run, ``decides``
layer, ``pending`` status, carrying the failed tool + error type. Absent an observability
context it is a silent no-op (best-effort). Both fully fail-soft — never breaking the recovery.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — recovery_attempted audit test skipped",
)


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


def _seed_tenant(pool) -> UUID:
    tid = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (str(tid), f"ra-{str(tid)[:8]}"),
        )
    return tid


def _recovery_rows(pool, run_id: UUID) -> list:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT event_layer, event_kind, actor, status, severity, decision "
            "FROM tm_audit_log WHERE run_id = %s AND event_kind = 'recovery_attempted'",
            (str(run_id),),
        ).fetchall()


def test_emit_recovery_attempted_writes_scoped_row(pool):
    from orchestrator.agent.orchestrator_agent import _emit_recovery_attempted
    from orchestrator.observability.decorators import observability_context

    tid = _seed_tenant(pool)
    rid = uuid4()
    with observability_context(run_id=rid, tenant_id=tid):
        _emit_recovery_attempted("spawn_sales_recovery", ValueError("handoff boom"))
    rows = _recovery_rows(pool, rid)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_layer"] == "decides"
    assert row["actor"] == "team_manager"
    assert row["status"] == "pending"
    assert row["severity"] == "warning"
    assert row["decision"]["failed_tool"] == "spawn_sales_recovery"
    assert row["decision"]["error_type"] == "ValueError"


def test_emit_recovery_attempted_noop_without_context(pool):
    """No ambient observability context → best-effort no-op (no row, no raise)."""
    from orchestrator.agent.orchestrator_agent import _emit_recovery_attempted

    rid = uuid4()
    _emit_recovery_attempted("spawn_x", RuntimeError("no ctx"))  # must not raise
    assert _recovery_rows(pool, rid) == []
