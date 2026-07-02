"""VT-564 — customer-send DELIVERY reconciliation (live Postgres).

"sent" (agent_drafts.status='sent' + the agent_customer_contacts ledger row) records transport
ACCEPTANCE, not delivery. This proves the async-callback reconciler closes the gap:

- a 'failed' / 'undelivered' callback stamps agent_customer_contacts.delivery_status, emits a
  send_result status-UPDATE audit, and fires the reviewer outbound_failure alert;
- a 'delivered' / 'read' callback records positive evidence (no alert);
- the FIRST delivery callback wins (terminal-safe) — a later callback is a no-op;
- an unknown / non-customer-send sid is a silent fail-soft no-op;
- pre_filter now routes delivered/read/undelivered to the reconciler and 'failed' to
  template_error_handler (which ALSO reconciles) WITHOUT regressing the owner error-notification;
- the send-time draft carries its originating run_id (threaded into the four send_result emits).

Mirrors owner_surface/test_owner_notification.py (the VT-524/534 owner analog) + the
agents/test_customer_send.py DB substrate. No live Twilio; no LLM (Pillar 1).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards

from orchestrator.agents import customer_send  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-564 delivery-reconciliation tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. mig 161) + launch DBOS so get_pool()/tenant_connection resolve."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _seed_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'standard', 'trial') RETURNING id",
            (f"vt564-{uuid4().hex[:8]}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_customer(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, "
            "complaint_status) VALUES (%s, 'Ravi', %s, 'subscribed', 'none') RETURNING id",
            (str(tenant), f"+9197{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_contact(
    dsn: str,
    tenant: UUID,
    customer: UUID,
    sid: str,
    *,
    draft_id: UUID | None = None,
    batch_id: UUID | None = None,
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_customer_contacts "
            "(tenant_id, customer_id, agent, draft_id, batch_id, template_name, message_sid) "
            "VALUES (%s, %s, 'sales_recovery', %s, %s, 'team_winback_simple', %s) RETURNING id",
            (
                str(tenant), str(customer),
                str(draft_id) if draft_id else None,
                str(batch_id) if batch_id else None,
                sid,
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_run(dsn: str, tenant: UUID) -> UUID:
    run_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), str(tenant)),
        )
    return run_id


def _seed_work_item(dsn: str, tenant: UUID, *, run_id: UUID | None = None) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status, run_id) "
            "VALUES (%s, %s, 'sales_recovery', 'approved', %s) RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", str(run_id) if run_id else None),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, work_item: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'approved') RETURNING id",
            (str(tenant), str(work_item)),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(dsn: str, tenant: UUID, batch: UUID, customer: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, status, "
            "message_sid) VALUES (%s, %s, %s, 'team_winback_simple', 'sent', %s) RETURNING id",
            (str(tenant), str(batch), str(customer), "SM" + uuid4().hex[:30]),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _contact_delivery(dsn: str, tenant: UUID, sid: str) -> tuple[str | None, object]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT delivery_status, delivery_updated_at FROM agent_customer_contacts "
            "WHERE tenant_id = %s AND message_sid = %s",
            (str(tenant), sid),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _send_result_audits(dsn: str, tenant: UUID) -> list[dict[str, object]]:
    """The reconcile send_result audit rows for a tenant (newest first)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT run_id::text AS run_id, result->>'status' AS rstatus, "
            "       result->>'delivery_status' AS dstatus, status AS audit_status, severity "
            "FROM tm_audit_log "
            "WHERE tenant_id = %s AND event_kind = 'send_result' "
            "  AND result->>'delivery_status' IS NOT NULL "
            "ORDER BY created_at DESC, id DESC",
            (str(tenant),),
        ).fetchall()
    return [
        {"run_id": r[0], "rstatus": r[1], "dstatus": r[2], "audit_status": r[3], "severity": r[4]}
        for r in rows
    ]


def _spy_dispatch(monkeypatch) -> list:
    """Capture dispatch_alert calls — the reconciler imports it lazily from this module."""
    from orchestrator.alerts import dispatch as dispatch_mod

    fired: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda trig: fired.append(trig) or None)
    return fired


# --- reconciler: failure path (stamp + audit + alert) ------------------------


@requires_db
def test_failed_callback_reconciles(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    result = customer_send.reconcile_customer_send_delivery(tenant, sid, "failed")

    assert result.matched is True
    assert result.delivery_status == "failed"
    status, updated_at = _contact_delivery(substrate.dsn, tenant, sid)
    assert status == "failed"
    assert updated_at is not None
    # send_result status-UPDATE audit emitted (delivery_failed / error).
    audits = _send_result_audits(substrate.dsn, tenant)
    assert audits and audits[0]["rstatus"] == "delivery_failed"
    assert audits[0]["dstatus"] == "failed"
    assert audits[0]["audit_status"] == "error"
    # reviewer alert fired, discriminated as a customer-send failure.
    assert len(fired) == 1
    assert fired[0].trigger_kind == "outbound_failure"
    assert fired[0].severity == "critical"
    assert fired[0].payload["surface"] == "customer_send"
    assert sid in fired[0].message_text


@requires_db
def test_undelivered_callback_reconciles(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """'undelivered' now routes + reconciles (was RouteToBrain) — a delivery failure like 'failed'."""
    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    result = customer_send.reconcile_customer_send_delivery(tenant, sid, "undelivered")

    assert result.matched is True
    assert result.delivery_status == "undelivered"
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "undelivered"
    assert len(fired) == 1
    assert fired[0].payload["delivery_status"] == "undelivered"


# --- reconciler: delivered path (record, no alert) ---------------------------


@requires_db
def test_delivered_callback_records_no_alert(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    result = customer_send.reconcile_customer_send_delivery(tenant, sid, "delivered")

    assert result.matched is True and result.delivery_status == "delivered"
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "delivered"
    assert fired == []
    audits = _send_result_audits(substrate.dsn, tenant)
    assert audits and audits[0]["rstatus"] == "delivered"
    assert audits[0]["audit_status"] == "ok"


@requires_db
def test_read_callback_maps_to_delivered(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    result = customer_send.reconcile_customer_send_delivery(tenant, sid, "read")

    assert result.delivery_status == "delivered"
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "delivered"


# --- reconciler: fail-soft + terminal-safety ---------------------------------


@requires_db
def test_unknown_sid_is_noop_fail_soft(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A callback for a sid with no customer-send row is a silent no-op — no raise, no alert."""
    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)

    result = customer_send.reconcile_customer_send_delivery(tenant, "SM" + uuid4().hex[:30], "failed")

    assert result.matched is False
    assert fired == []


@requires_db
def test_unknown_state_and_missing_sid_are_noops(substrate):  # type: ignore[no-untyped-def]
    tenant = _seed_tenant(substrate.dsn)
    assert customer_send.reconcile_customer_send_delivery(tenant, "SMx", "queued").matched is False
    assert customer_send.reconcile_customer_send_delivery(tenant, None, "failed").matched is False
    assert customer_send.reconcile_customer_send_delivery(tenant, "SMx", None).matched is False


@requires_db
def test_terminal_safe_first_callback_wins(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """First delivery callback wins — a later 'delivered' must NOT overwrite 'failed', and the
    redelivered callback fires no second alert (rowcount-0 no-op)."""
    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    first = customer_send.reconcile_customer_send_delivery(tenant, sid, "failed")
    second = customer_send.reconcile_customer_send_delivery(tenant, sid, "delivered")  # no-op

    assert first.matched is True and second.matched is False
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "failed"
    assert len(fired) == 1  # only the first (failed) transition alerted


# --- run_id threading --------------------------------------------------------


@requires_db
def test_reconcile_audit_carries_run_id(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The reconcile send_result audit correlates back to the send's run_id (contact → batch →
    work_item.run_id)."""
    _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    run_id = _seed_run(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant, run_id=run_id)
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid, batch_id=batch)

    customer_send.reconcile_customer_send_delivery(tenant, sid, "failed")

    audits = _send_result_audits(substrate.dsn, tenant)
    assert audits and audits[0]["run_id"] == str(run_id)


@requires_db
def test_load_draft_threads_run_id(substrate):  # type: ignore[no-untyped-def]
    """_load_draft carries the originating run_id (the value all four send-time send_result emits
    now pass) — it resolves through batch → work_item.run_id."""
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    run_id = _seed_run(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant, run_id=run_id)
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)

    with psycopg.connect(substrate.dsn) as conn:
        loaded = customer_send._load_draft(conn, str(tenant), str(draft))

    assert loaded is not None
    assert loaded["run_id"] == str(run_id)


@requires_db
def test_load_draft_run_id_null_when_no_work_item_run(substrate):  # type: ignore[no-untyped-def]
    """A work item with no run_id (or a legacy draft) leaves run_id NULL — the LEFT JOIN never
    drops the draft."""
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)  # run_id NULL
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)

    with psycopg.connect(substrate.dsn) as conn:
        loaded = customer_send._load_draft(conn, str(tenant), str(draft))

    assert loaded is not None and loaded["run_id"] is None


# --- handler + pre_filter integration ----------------------------------------


@requires_db
def test_delivery_handler_routes_and_reconciles(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The registered direct handler reconciles an undelivered callback via the ledger."""
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    fired = _spy_dispatch(monkeypatch)
    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    event = WebhookEvent(
        message_type="status_callback", status_callback_state="undelivered", twilio_message_sid=sid
    )
    outcome = HANDLERS["customer_send_delivery_handler"](event, new_subscriber_state(tenant))

    assert outcome["handler"] == "customer_send_delivery_handler"
    assert outcome["reconciled"] is True
    assert outcome["delivery_status"] == "undelivered"
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "undelivered"
    assert len(fired) == 1


@requires_db
def test_template_error_handler_reconciles_and_still_notifies_owner(
    substrate, monkeypatch
):  # type: ignore[no-untyped-def]
    """A 'failed' callback still sends the owner error-notification (unregressed) AND reconciles
    the customer-send ledger."""
    import importlib

    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    # importlib (not `import a.b.c as teh`): the package __init__ rebinds the
    # `template_error_handler` attribute to the FUNCTION, shadowing the submodule.
    teh = importlib.import_module("orchestrator.direct_handlers.template_error_handler")

    _spy_dispatch(monkeypatch)
    owner_sends: list = []

    class _FakeSendResult:
        def model_dump(self) -> dict[str, object]:
            return {"success": True, "template_name": "team_error_handler"}

    def _fake_send(tenant_id, template_name, params):  # type: ignore[no-untyped-def]
        owner_sends.append(template_name)
        return _FakeSendResult()

    monkeypatch.setattr(teh, "send_template_message", _fake_send)

    tenant = _seed_tenant(substrate.dsn)
    customer = _seed_customer(substrate.dsn, tenant)
    sid = "SM" + uuid4().hex[:30]
    _seed_contact(substrate.dsn, tenant, customer, sid)

    event = WebhookEvent(
        message_type="status_callback", status_callback_state="failed", twilio_message_sid=sid
    )
    outcome = teh.template_error_handler(event, new_subscriber_state(tenant))

    # unregressed: owner notification still attempted + the existing contract keys.
    assert owner_sends == ["team_error_handler"]
    assert outcome["retry_eligible"] is True
    assert outcome["send_result"]["template_name"] == "team_error_handler"
    # AND the customer-send delivery is reconciled.
    assert outcome["reconciled"] is True
    assert _contact_delivery(substrate.dsn, tenant, sid)[0] == "failed"


@requires_db
def test_prefilter_routes_delivery_callbacks_to_reconciler(substrate):  # type: ignore[no-untyped-def]
    """pre_filter routes delivered/read/undelivered → the reconciler and 'failed' →
    template_error_handler (the reconcile-inside path)."""
    from orchestrator.pre_filter_gate import pre_filter
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import RouteToDirectHandler, WebhookEvent

    tenant = _seed_tenant(substrate.dsn)
    state = new_subscriber_state(tenant)
    for cb in ("delivered", "read", "undelivered"):
        event = WebhookEvent(
            message_type="status_callback", status_callback_state=cb, twilio_message_sid="SMx"
        )
        result = pre_filter(event, state)
        assert isinstance(result, RouteToDirectHandler)
        assert result.handler_name == "customer_send_delivery_handler"
    failed_event = WebhookEvent(
        message_type="status_callback", status_callback_state="failed", twilio_message_sid="SMx"
    )
    failed = pre_filter(failed_event, state)
    assert isinstance(failed, RouteToDirectHandler)
    assert failed.handler_name == "template_error_handler"
