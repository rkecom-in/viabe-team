"""VT-3.3c tests — direct handlers send a Twilio template and report the
outcome honestly via send_result (audit C4, CL-74).

Require a live Postgres via ``DATABASE_URL`` plus the dbos / twilio stack;
run in the CI ``orchestrator`` job. Twilio is stubbed (conftest.py) — no live
call. dupe_handler is not covered here: it sends nothing.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("twilio")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — direct-handler tests skipped",
)


@pytest.fixture(scope="module")
def handlers_ctx():
    """Apply migrations + launch DBOS; expose the handler registry."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    launch_dbos()
    try:
        yield SimpleNamespace(
            dsn=dsn,
            HANDLERS=HANDLERS,
            make_state=new_subscriber_state,
            WebhookEvent=WebhookEvent,
        )
    finally:
        shutdown_dbos()


def _phone() -> str:
    return f"+9199{uuid4().int % 10**8:08d}"


def _new_tenant(dsn: str, whatsapp_number: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number) VALUES ('VT-3.3c Handler Test', 'founding', 'trial', "
            "now(), %s) RETURNING id",
            (whatsapp_number,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _force_4xx(twilio_create) -> None:
    """Make the stubbed Twilio send fail with a permanent (4xx) error."""
    from twilio.base.exceptions import TwilioRestException

    twilio_create.side_effect = TwilioRestException(
        status=400, uri="/Messages", msg="permanent failure", code=21610
    )


# --- opt_out_handler ---------------------------------------------------------


def test_opt_out_handler_returns_truthful_send_result(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="STOP", sender_phone=phone)

    outcome = ctx.HANDLERS["opt_out_handler"](event, state)
    assert outcome["opt_out_set"] is True
    assert outcome["send_result"]["success"] is True
    assert outcome["send_result"]["template_name"] == "team_opt_out_confirmation"

    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row["opt_out"] is True


def test_opt_out_handler_handles_send_failure(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    _force_4xx(twilio_create)
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="STOP", sender_phone=phone)

    outcome = ctx.HANDLERS["opt_out_handler"](event, state)
    # Side effect persisted even though the send failed — reported honestly.
    assert outcome["opt_out_set"] is True
    assert outcome["send_result"]["success"] is False
    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row["opt_out"] is True


# --- dsr_handler -------------------------------------------------------------


def test_dsr_handler_returns_truthful_send_result(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="please delete my data", sender_phone=phone)

    outcome = ctx.HANDLERS["dsr_handler"](event, state)
    assert outcome["dsr_ticket_id"] is not None
    assert outcome["send_result"]["success"] is True
    assert outcome["send_result"]["template_name"] == "team_dsr_acknowledgment"

    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM dsr_tickets WHERE id = %s",
            (outcome["dsr_ticket_id"],),
        ).fetchone()
    assert row["status"] == "acknowledged"


def test_dsr_handler_handles_send_failure(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    _force_4xx(twilio_create)
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="please delete my data", sender_phone=phone)

    outcome = ctx.HANDLERS["dsr_handler"](event, state)
    # The ticket was still created even though the acknowledgment send failed.
    assert outcome["dsr_ticket_id"] is not None
    assert outcome["send_result"]["success"] is False
    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM dsr_tickets WHERE id = %s",
            (outcome["dsr_ticket_id"],),
        ).fetchone()
    assert row["status"] == "acknowledged"


# --- status_ping_handler -----------------------------------------------------


def test_status_ping_handler_returns_truthful_send_result(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="hi", sender_phone=phone)

    outcome = ctx.HANDLERS["status_ping_handler"](event, state)
    assert outcome["send_result"]["success"] is True
    assert outcome["send_result"]["template_name"] == "team_status_ping"
    assert "trial" in outcome["status_text"]  # accurate phase, no fabrication


def test_status_ping_handler_handles_send_failure(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    _force_4xx(twilio_create)
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="hi", sender_phone=phone)

    outcome = ctx.HANDLERS["status_ping_handler"](event, state)
    # The accurate state was still computed even though the send failed.
    assert "trial" in outcome["status_text"]
    assert outcome["send_result"]["success"] is False


# --- template_error_handler --------------------------------------------------


def test_template_error_handler_returns_truthful_send_result(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    tenant_id = _new_tenant(ctx.dsn, _phone())
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(
        message_type="status_callback",
        status_callback_state="failed",
        twilio_message_sid="SMfailed",
    )

    outcome = ctx.HANDLERS["template_error_handler"](event, state)
    assert outcome["retry_eligible"] is True
    assert outcome["send_result"]["success"] is True
    assert outcome["send_result"]["template_name"] == "team_error_handler"


def test_template_error_handler_handles_send_failure(handlers_ctx, twilio_create):
    ctx = handlers_ctx
    _force_4xx(twilio_create)
    tenant_id = _new_tenant(ctx.dsn, _phone())
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(
        message_type="status_callback",
        status_callback_state="failed",
        twilio_message_sid="SMfailed",
    )

    outcome = ctx.HANDLERS["template_error_handler"](event, state)
    # retry-eligibility is still recorded even though the owner send failed.
    assert outcome["retry_eligible"] is True
    assert outcome["send_result"]["success"] is False
