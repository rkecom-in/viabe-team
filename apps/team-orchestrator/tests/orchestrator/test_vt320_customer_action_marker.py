"""VT-320 — agent-action customer marker + the act→reconstitute loop.

When the agent ACTS on a customer (a campaign send, D1), `record_customer_action_marker`
writes a customer-referencing episodic row (`referenced_entity_type='customer'`).
This proves the loop Cowork asked for: the marker lands such a row, it's idempotent
per (customer, campaign), and VT-76's `reconstitute_customer` then anonymizes it to
the sentinel — i.e. the reconstitution sweep is NON-degenerate (has real rows to
act on). CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-320 marker canary skipped",
)


def _pool():
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=2,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    return _pool()


def _rows(pool, tid: str) -> list[dict]:
    with pool.connection() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT event_type, referenced_entity_type, referenced_entity_id "
                "FROM episodic_events WHERE tenant_id = %s "
                "AND event_type = 'customer_action_taken'", (tid,),
            ).fetchall()
        ]


def test_marker_lands_customer_ref_then_reconstitute_anonymizes(pool):
    from orchestrator.knowledge.l2_writer import record_customer_action_marker
    from orchestrator.privacy.reconstitution import (
        RECONSTITUTION_SENTINEL,
        reconstitute_customer,
    )

    tid, cid, campaign = str(uuid4()), str(uuid4()), str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'vt320', 'founding', 'paid_active')", (tid,),
        )
        conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name) "
            "VALUES (%s, %s, 'Cust')", (cid, tid),
        )

    # ACT: the agent acts on the customer → marker.
    record_customer_action_marker(tid, cid, action="campaign_send", dedup_source=campaign)
    # Idempotent per (customer, campaign): a re-send must NOT double-mark.
    record_customer_action_marker(tid, cid, action="campaign_send", dedup_source=campaign)

    rows = _rows(pool, tid)
    assert len(rows) == 1, "marker must be idempotent per (customer, campaign)"
    assert rows[0]["referenced_entity_type"] == "customer"
    assert str(rows[0]["referenced_entity_id"]) == cid, "row must reference the customer"

    # RECONSTITUTE (VT-76): the opt-out sweep anonymizes the customer's footprint.
    n = reconstitute_customer(tid, cid)
    assert n >= 1, "reconstitution must find the marker row (non-degenerate)"

    after = _rows(pool, tid)
    assert len(after) == 1, "the row stays (audit / k-anon integrity)"
    assert after[0]["referenced_entity_id"] == RECONSTITUTION_SENTINEL, "ref must be the sentinel"
    assert str(after[0]["referenced_entity_id"]) != cid, "customer link must be gone"
