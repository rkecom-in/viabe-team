"""VT-65 PR-1 — KG population consumer + backfill substrate tests.

Live Postgres via DATABASE_URL (CI orchestrator job).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — KG population tests skipped",
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
            (tid, f"vt65t-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def _ent_count(pool, tid: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s AND external_key IS NOT NULL",
            (tid,),
        ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def test_customer_event_creates_node_and_owns_edge(pool):
    from orchestrator.knowledge.kg_population import KgEvent, process_kg_event
    from orchestrator.utils.phone_token import hash_phone
    from uuid import UUID

    tid = _tenant(pool)
    cid = str(uuid4())
    phone = "+919812345678"
    result = process_kg_event(KgEvent(uuid4(), "customer_created", UUID(tid),
                                      {"customer_id": cid, "phone_e164": phone}))
    assert result == "processed"
    with pool.connection() as conn:
        ent = conn.execute(
            "SELECT attributes FROM l1_entities WHERE tenant_id = %s AND entity_type = 'customer' "
            "AND external_key = %s", (tid, cid),
        ).fetchone()
        edge = conn.execute(
            "SELECT count(*) AS n FROM l1_relationships WHERE tenant_id = %s AND relationship_type = 'owns'",
            (tid,),
        ).fetchone()
    attrs = ent["attributes"] if isinstance(ent, dict) else ent[0]
    assert attrs["phone_hash"] == hash_phone(phone)  # canonical hash
    assert phone not in str(attrs)  # CL-390 no raw phone
    assert (edge["n"] if isinstance(edge, dict) else edge[0]) == 1


def test_duplicate_event_is_skipped(pool):
    from orchestrator.knowledge.kg_population import KgEvent, process_kg_event
    from uuid import UUID

    tid = _tenant(pool)
    eid = uuid4()
    ev = KgEvent(eid, "customer_created", UUID(tid), {"customer_id": str(uuid4())})
    assert process_kg_event(ev) == "processed"
    assert process_kg_event(ev) == "skipped"  # idempotent on event_id


def test_unknown_event_fails_without_crash(pool):
    from orchestrator.knowledge.kg_population import KgEvent, process_kg_event
    from uuid import UUID

    tid = _tenant(pool)
    result = process_kg_event(KgEvent(uuid4(), "not_a_real_event", UUID(tid), {}))
    assert result == "failed"  # recorded, not raised


def test_handler_failure_recorded_not_raised(pool):
    """A handler that hits bad data records 'failed' + never raises (spec §5)."""
    from orchestrator.knowledge.kg_population import KgEvent, process_kg_event
    from uuid import UUID

    tid = _tenant(pool)
    # customer_created without customer_id → KeyError inside the handler → caught.
    result = process_kg_event(KgEvent(uuid4(), "customer_created", UUID(tid), {}))
    assert result == "failed"


def test_backfill_populates_and_is_idempotent(pool):
    from orchestrator.knowledge.kg_backfill import backfill_tenant

    tid = _tenant(pool)
    cid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name, phone_e164) VALUES (%s,%s,%s,%s)",
            (cid, tid, "Test", "+919800000001"),
        )
    backfill_tenant(tid)
    first = _ent_count(pool, tid)
    assert first >= 2  # tenant + customer
    backfill_tenant(tid)  # re-run
    assert _ent_count(pool, tid) == first  # idempotent
