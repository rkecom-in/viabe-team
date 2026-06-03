"""VT-65 PR-2 — transactional outbox emit + drain substrate tests.

Live Postgres via DATABASE_URL (CI orchestrator job). Proves the atomicity the
outbox exists for: commit→drain→KG, rollback→none, idempotent re-drain.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — KG emit tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"emit-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def _customer_nodes(pool, tid: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s AND entity_type='customer'",
            (tid,),
        ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def test_commit_emit_drain_to_kg(pool):
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event

    tid = _tenant(pool)
    with tenant_connection(UUID(tid)) as conn, conn.transaction():
        emit_kg_event(conn, "customer_created", tid, {"customer_id": str(uuid4())})
    out = drain_kg_events(tid)
    assert out["drained"] == 1
    assert _customer_nodes(pool, tid) == 1


def test_rollback_emits_nothing(pool):
    """Atomicity: an emit in a rolled-back txn leaves NO outbox row + NO KG."""
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event

    tid = _tenant(pool)
    rk = str(uuid4())
    with pytest.raises(RuntimeError):
        with tenant_connection(UUID(tid)) as conn, conn.transaction():
            emit_kg_event(conn, "customer_created", tid, {"customer_id": rk})
            raise RuntimeError("boom")
    drain_kg_events(tid)
    with pool.connection() as conn:
        outbox = conn.execute(
            "SELECT count(*) AS n FROM kg_events WHERE tenant_id = %s", (tid,)
        ).fetchone()
    assert (outbox["n"] if isinstance(outbox, dict) else outbox[0]) == 0
    assert _customer_nodes(pool, tid) == 0


def test_idempotent_redrain(pool):
    from orchestrator.db import tenant_connection
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event

    tid = _tenant(pool)
    with tenant_connection(UUID(tid)) as conn, conn.transaction():
        emit_kg_event(conn, "customer_created", tid, {"customer_id": str(uuid4())})
    drain_kg_events(tid)
    n1 = _customer_nodes(pool, tid)
    drain_kg_events(tid)  # re-drain
    assert _customer_nodes(pool, tid) == n1
