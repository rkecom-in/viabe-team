"""VT-606 amendment A3 — stale-resume re-engagement (live Postgres for the last-inbound read +
incident write; the actual Twilio send is mocked — no real WhatsApp send)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — stale_resume DB tests skipped",
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
            "INSERT INTO tenants (id, business_name, plan_tier, phase, owner_phone) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"sr-{tid[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        )
    return tid


def test_last_owner_inbound_at_reads_the_conversation_log(pool):
    from orchestrator.manager.stale_resume import last_owner_inbound_at

    tid = _seed_tenant(pool)
    assert last_owner_inbound_at(tid) is None  # never messaged

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO conversation_log (tenant_id, role, text, surface) "
            "VALUES (%s, 'owner', 'hi', 'manager')",
            (tid,),
        )
    at = last_owner_inbound_at(tid)
    assert at is not None
    assert (datetime.now(timezone.utc) - at) < timedelta(minutes=1)


def test_reengage_stale_task_sends_via_owner_template_seam(pool, monkeypatch: pytest.MonkeyPatch):
    """VT-683 point B: the stale-resume caller now sends team_wakeup2 (team_reengage merged in) via
    the SHARED wake-up helper → owner_send.send_owner_template. pending_count = the tenant's queued
    owner-comms count, floored to 1 (this fresh tenant has none → '1')."""
    from datetime import datetime as _dt

    import orchestrator.manager.stale_resume as sr
    import orchestrator.owner_surface.owner_send as owner_send_mod
    from orchestrator.utils.twilio_send import SendResult

    tid = _seed_tenant(pool)
    task_id = uuid4()

    sent = {}

    def _fake_send_owner_template(tenant_id, template_name, language, params, *, recipient_phone):
        sent["template_name"] = template_name
        sent["params"] = params
        sent["recipient_phone"] = recipient_phone
        return SendResult(
            success=True, message_sid="SMtest123", attempted_at=_dt.now(timezone.utc),
            template_name=template_name, recipient_phone_token="tok_xxx",
        )

    # reengage → wakeup.send_wakeup → owner_send.send_owner_template (patch the real send seam).
    monkeypatch.setattr(owner_send_mod, "send_owner_template", _fake_send_owner_template)

    result = sr.reengage_stale_task(tid, task_id, owner_phone="+919876543210", owner_name="Test Owner")

    assert result is not None
    assert result.success is True
    assert sent["template_name"] == "team_wakeup2"
    assert sent["params"] == {"owner_name": "Test Owner", "pending_count": "1"}
    assert sent["recipient_phone"] == "+919876543210"


def test_reengage_stale_task_send_failure_raises_incident(pool, monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime as _dt

    import orchestrator.manager.stale_resume as sr
    import orchestrator.owner_surface.owner_send as owner_send_mod
    from orchestrator.observability.incident_store import get_incident
    from orchestrator.utils.twilio_send import SendResult

    tid = _seed_tenant(pool)
    task_id = uuid4()

    def _failing_send(tenant_id, template_name, language, params, *, recipient_phone):
        return SendResult(
            success=False, error_code="63016", error_message="outside window",
            attempted_at=_dt.now(timezone.utc), template_name=template_name,
            recipient_phone_token="tok_xxx",
        )

    monkeypatch.setattr(owner_send_mod, "send_owner_template", _failing_send)

    result = sr.reengage_stale_task(tid, task_id, owner_phone="+919876543210")
    assert result is not None
    assert result.success is False

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM incidents WHERE tenant_id = %s AND run_id = %s", (tid, str(task_id))
        ).fetchone()
    assert row is not None
    incident = get_incident(tid, row["id"])
    assert incident is not None
    assert incident["incident_kind"] == "owner_unreachable"
