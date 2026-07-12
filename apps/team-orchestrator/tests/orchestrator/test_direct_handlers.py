"""VT-3.3c tests — direct handlers send a Twilio template and report the
outcome honestly via send_result (audit C4, CL-74).

Require a live Postgres via ``DATABASE_URL`` plus the dbos / twilio stack;
run in the CI ``orchestrator`` job. Twilio is stubbed (conftest.py) — no live
call. dupe_handler is not covered here: it sends nothing.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
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


def _new_tenant(dsn: str, whatsapp_number: str, *, ownership_verified: bool = True) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number, ownership_verified) VALUES ('VT-3.3c Handler Test', 'founding', 'trial', "
            "now(), %s, %s) RETURNING id",
            (whatsapp_number, ownership_verified),
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
    assert row[0] is True  # bare psycopg.connect -> tuple rows


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
    assert row[0] is True  # bare psycopg.connect -> tuple rows


# --- data_inputs_enable_handler (D1a — ACTIVATE TEAM clears opt_out) ----------


def test_data_inputs_enable_clears_opt_out(handlers_ctx, twilio_create):
    """D1a: ACTIVATE TEAM re-consent sets owner_inputs=true AND clears opt_out (the sole clearer,
    symmetric to opt_out_handler the sole setter). An opted-out tenant is re-activated."""
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    # Simulate a prior STOP: opted out + inputs disabled.
    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE tenants SET opt_out = true, owner_inputs = false WHERE id = %s", (tenant_id,)
        )
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="ACTIVATE TEAM", sender_phone=phone)

    outcome = ctx.HANDLERS["data_inputs_enable_handler"](event, state)
    assert outcome["owner_inputs_set"] is True

    with psycopg.connect(ctx.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT owner_inputs, opt_out FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    assert row[0] is True   # owner_inputs re-enabled
    assert row[1] is False  # opt_out CLEARED — re-consent retracts the STOP


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
    assert row[0] == "acknowledged"  # bare psycopg.connect -> tuple rows


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
    assert row[0] == "acknowledged"  # bare psycopg.connect -> tuple rows


def _split_dsr_sends(twilio_create):
    """Split the mock's recorded ``messages.create`` calls into the scope-confirmation freeform
    (passes ``body=``) and the Meta acknowledgment template (passes ``content_sid=`` +
    ``content_variables=``). Returns ``(freeform_call, template_call, freeform_i, template_i)`` with
    ``None`` for any leg not sent."""
    freeform = template = None
    freeform_i = template_i = None
    for i, call in enumerate(twilio_create.call_args_list):
        if "body" in call.kwargs:
            freeform, freeform_i = call, i
        elif "content_sid" in call.kwargs:
            template, template_i = call, i
    return freeform, template, freeform_i, template_i


def test_dsr_handler_scope_confirmation_before_template_with_real_params(handlers_ctx, twilio_create):
    """R8: the DPDP acknowledgment now fills all three declared params (VT-400 class fix — no more
    Twilio SAMPLE values to a real owner) AND a deterministic plain-language scope confirmation is
    sent BEFORE the Meta template (RC1 chat-summary-before-template pattern)."""
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="please delete my data", sender_phone=phone)

    outcome = ctx.HANDLERS["dsr_handler"](event, state)

    freeform, template, freeform_i, template_i = _split_dsr_sends(twilio_create)
    assert freeform is not None, "scope confirmation freeform was not sent"
    assert template is not None, "acknowledgment template was not sent"
    # ORDER: the scope confirmation is sent BEFORE the Meta template.
    assert freeform_i < template_i

    # The scope confirmation names deletion + account closure + automation-frozen + the deadline.
    body = freeform.kwargs["body"]
    assert "deleted" in body
    assert "account closed" in body
    assert "frozen" in body

    # VT-400 fix: all THREE declared params filled (config/twilio_templates.yaml team_dsr_acknowledgment
    # variables owner_name/dsr_type/completion_deadline_date -> positional {"1","2","3"}), none empty.
    content_variables = json.loads(template.kwargs["content_variables"])
    assert set(content_variables) == {"1", "2", "3"}
    assert all(str(content_variables[k]).strip() for k in ("1", "2", "3"))
    # owner_name is the seeded business_name; dsr_type comes from the ticket ('deletion').
    assert content_variables["1"] == "VT-3.3c Handler Test"
    assert content_variables["2"] == "deletion"
    # completion_deadline_date ~30 days out, and the SAME date string appears in the scope confirmation.
    deadline = datetime.strptime(content_variables["3"], "%d %B %Y").date()
    days_out = (deadline - datetime.now(UTC).date()).days
    assert 29 <= days_out <= 31
    assert content_variables["3"] in body

    # The return contract reports both sends honestly.
    assert outcome["scope_confirmation"]["sid"] is not None
    assert outcome["scope_confirmation"]["error"] is None
    assert outcome["send_result"]["template_name"] == "team_dsr_acknowledgment"
    assert outcome["send_result"]["success"] is True


def test_dsr_handler_skips_scope_confirmation_when_no_sender_phone(handlers_ctx, twilio_create):
    """Best-effort guard: with no sender phone on the event, the freeform scope confirmation is
    skipped and honestly reported, while the ticket + freeze + template ack (which falls back to the
    tenant's own whatsapp_number) still stand — the DSR is never blocked."""
    ctx = handlers_ctx
    phone = _phone()
    tenant_id = _new_tenant(ctx.dsn, phone)
    state = ctx.make_state(UUID(tenant_id))
    event = ctx.WebhookEvent(body="please delete my data", sender_phone="")  # no sender phone

    outcome = ctx.HANDLERS["dsr_handler"](event, state)

    freeform, template, _, _ = _split_dsr_sends(twilio_create)
    assert freeform is None  # no in-session scope confirmation without a recipient
    assert template is not None  # the ack still sends (tenant whatsapp_number fallback)
    assert outcome["dsr_ticket_id"] is not None
    assert outcome["scope_confirmation"]["sid"] is None
    assert outcome["scope_confirmation"]["error"] == "no recipient phone on event"
    assert outcome["send_result"]["success"] is True


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
