"""VT-524 (B1) — owner-notification delivery ledger tests (live Postgres).

Proves the delivery-truth path that closes the VT-519 blindness: a send records
'accepted' (transport SID in hand); the async status callback flips it to
delivered/failed; the first terminal callback wins (no regression); a callback for
an unknown sid is a silent no-op.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — owner_notification tests skipped",
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


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"on-{tid[:8]}"),
        )
    return tid


def _row(pool, sid: str):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT owner_notification_status AS s, communication_status AS c, "
            "accepted_at, delivered_at, failed_at "
            "FROM owner_notifications WHERE message_sid = %s",
            (sid,),
        ).fetchone()


def test_record_owner_notification_accepted(pool):
    from orchestrator.owner_surface.owner_notification import record_owner_notification

    tid = _seed_tenant(pool)
    sid = f"SM{uuid4().hex}"
    record_owner_notification(tid, "team_welcome3", sid)
    row = _row(pool, sid)
    assert row is not None
    assert row["s"] == "accepted"
    assert row["accepted_at"] is not None
    assert row["delivered_at"] is None and row["failed_at"] is None


def test_delivered_callback_marks_delivered(pool):
    from orchestrator.owner_surface.owner_notification import (
        record_owner_notification,
        record_owner_notification_delivery,
    )

    tid = _seed_tenant(pool)
    sid = f"SM{uuid4().hex}"
    record_owner_notification(tid, "team_welcome3", sid)
    record_owner_notification_delivery(tid, sid, "delivered")
    row = _row(pool, sid)
    assert row["s"] == "delivered"
    assert row["c"] == "delivered"
    assert row["delivered_at"] is not None


def test_undelivered_callback_marks_failed_incident(pool):
    """The exact 63049 case: a send is accepted, then Meta declines delivery → 'undelivered'
    → owner_notification_status='failed', communication_status='failed_incident_open'."""
    from orchestrator.owner_surface.owner_notification import (
        record_owner_notification,
        record_owner_notification_delivery,
    )

    tid = _seed_tenant(pool)
    sid = f"SM{uuid4().hex}"
    record_owner_notification(tid, "team_welcome3", sid)
    record_owner_notification_delivery(tid, sid, "undelivered")
    row = _row(pool, sid)
    assert row["s"] == "failed"
    assert row["c"] == "failed_incident_open"
    assert row["failed_at"] is not None


def test_delivery_is_terminal_safe(pool):
    """First terminal callback wins — a later 'delivered' must NOT overwrite a 'failed'."""
    from orchestrator.owner_surface.owner_notification import (
        record_owner_notification,
        record_owner_notification_delivery,
    )

    tid = _seed_tenant(pool)
    sid = f"SM{uuid4().hex}"
    record_owner_notification(tid, "team_welcome3", sid)
    record_owner_notification_delivery(tid, sid, "failed")
    record_owner_notification_delivery(tid, sid, "delivered")  # must be a no-op
    row = _row(pool, sid)
    assert row["s"] == "failed"
    assert row["c"] == "failed_incident_open"


def test_unknown_sid_is_noop(pool):
    """A delivery callback for a sid with no recorded owner send is a silent no-op (fail-soft)."""
    from orchestrator.owner_surface.owner_notification import (
        record_owner_notification_delivery,
    )

    tid = _seed_tenant(pool)
    sid = f"SM{uuid4().hex}"
    record_owner_notification_delivery(tid, sid, "delivered")  # no raise
    assert _row(pool, sid) is None
