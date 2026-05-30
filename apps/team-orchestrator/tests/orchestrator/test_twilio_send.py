"""VT-3.3c tests — utils/twilio_send.send_template_message.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / twilio stack;
run in the CI ``orchestrator`` job. No live Twilio call is made — the client
is stubbed via the ``twilio_create`` fixture (conftest.py).
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
    reason="DATABASE_URL not set — twilio_send tests skipped",
)


@pytest.fixture(scope="module")
def send_ctx():
    """Apply migrations + launch DBOS so @DBOS.step / get_pool() work."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str, whatsapp_number: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number) VALUES ('VT-3.3c Send Test', 'founding', 'trial', "
            "now(), %s) RETURNING id",
            (whatsapp_number,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_send_returns_success_when_content_sid_present(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    result = send_template_message(
        uuid4(), "team_opt_out_confirmation", {}, recipient_phone="+919812300001"
    )
    assert result.success is True
    assert result.message_sid == "SM" + "0" * 32
    assert result.template_name == "team_opt_out_confirmation"
    twilio_create.assert_called_once()


def test_send_returns_stub_when_content_sid_null(send_ctx, twilio_create, monkeypatch):
    from types import SimpleNamespace

    from orchestrator.utils import twilio_send

    # VT-163: send_template_message resolves via the registry (_registry_resolve),
    # not the legacy _templates() dict. Monkeypatch the resolver to return a
    # registered template whose content_sid is null (pending Meta approval) so
    # the stub path is exercised.
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, lang="en": SimpleNamespace(content_sid=None, audience="owner"),
    )
    result = twilio_send.send_template_message(
        uuid4(), "team_pending", {}, recipient_phone="+919812300002"
    )
    assert result.success is False
    assert result.error_code == "template_not_yet_approved"
    twilio_create.assert_not_called()


def test_send_raises_when_template_not_in_yaml(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import (
        TemplateNotConfigured,
        send_template_message,
    )

    with pytest.raises(TemplateNotConfigured):
        send_template_message(
            uuid4(), "no_such_template", {}, recipient_phone="+919812300003"
        )


def test_send_handles_permanent_twilio_error_4xx(send_ctx, twilio_create):
    from twilio.base.exceptions import TwilioRestException

    from orchestrator.utils.twilio_send import send_template_message

    twilio_create.side_effect = TwilioRestException(
        status=400, uri="/Messages", msg="invalid 'To' number", code=21211
    )
    result = send_template_message(
        uuid4(), "team_status_ping", {}, recipient_phone="+919812300004"
    )
    assert result.success is False
    assert result.error_code == "21211"
    assert "invalid" in (result.error_message or "")


def test_send_propagates_transient_twilio_error_5xx(send_ctx, twilio_create):
    from twilio.base.exceptions import TwilioRestException

    from orchestrator.utils.twilio_send import send_template_message

    twilio_create.side_effect = TwilioRestException(
        status=503, uri="/Messages", msg="service unavailable", code=20500
    )
    with pytest.raises(TwilioRestException):
        send_template_message(
            uuid4(), "team_status_ping", {}, recipient_phone="+919812300005"
        )


def test_send_uses_tenant_whatsapp_number_by_default(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    number = "+919876543210"
    tenant_id = _new_tenant(send_ctx.dsn, number)
    send_template_message(UUID(tenant_id), "team_status_ping", {})
    assert twilio_create.call_args.kwargs["to"] == number


def test_send_uses_recipient_phone_override(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    override = "+919800000099"
    send_template_message(
        uuid4(), "team_status_ping", {}, recipient_phone=override
    )
    assert twilio_create.call_args.kwargs["to"] == override


def test_recipient_phone_is_tokenised_in_result(send_ctx, twilio_create):
    from orchestrator.utils.twilio_send import send_template_message

    phone = "+919811112222"
    result = send_template_message(
        uuid4(), "team_status_ping", {}, recipient_phone=phone
    )
    assert result.recipient_phone_token.startswith("phone_tok_")
    assert phone not in result.recipient_phone_token
