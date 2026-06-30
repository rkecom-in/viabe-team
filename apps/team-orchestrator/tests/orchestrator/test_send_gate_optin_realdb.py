"""VT-301 / CL-429 — WhatsApp send-gate canary (Rule #15, real Postgres).

Fazal ruled 2026-06-02: gate ALL business-initiated template sends on a
recorded WhatsApp opt-in. ``send_whatsapp_template`` now refuses fail-CLOSED
when ``privacy.consent.has_consent_for_phone`` is False, IN ADDITION to the
existing ``opt_out_status`` check.

No mock cursors — every send below runs through the production
``send_whatsapp_template`` path against a real Postgres (SET LOCAL
app.current_tenant + RLS), with only the Twilio leaf (``send_fn``) stubbed so
the canary never touches the live WABA. Gated on DATABASE_URL + the dbos stack,
mirroring test_consent_substrate.py; runs in the CI orchestrator job + the
pre-push orchestrator/migrations job. CL-422 synthetic data only; CL-390 no PII
(we assert on the refusal CODE, never surface the raw number).

Canary matrix (the VT-301 plan):
  - opted-in customer (consent recorded) -> send ALLOWED (status 'sent').
  - no consent record -> send REFUSED fail-closed ('recipient_not_opted_in'),
    send_fn never called.
  - opted-out customer -> REFUSED ('recipient_opted_out', the prior check).
  - cross-tenant: consent under tenant A does NOT authorize a send to the same
    phone under tenant B.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langchain")

import psycopg  # noqa: E402 — after dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-301 send-gate canary skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so get_pool()/tenant_connection exist."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt301-send-gate-test-salt")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


# --- helpers ---------------------------------------------------------------


class _OkSend:
    success = True
    message_sid = "MK" + "a" * 30
    error_code = None
    error_message = None


def _ok_send_fn(*_a, **_k) -> _OkSend:
    return _OkSend()


def _synthetic_phone() -> str:
    """A synthetic E.164 number (CL-422: dev = synthetic only)."""
    return f"+9197{uuid4().int % 10**8:08d}"


def _new_tenant(dsn: str, *, name: str = "VT301 send-gate", ownership_verified: bool = True) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, whatsapp_number, ownership_verified) "
            "VALUES (%s, 'founding', 'paid_active', %s, %s) RETURNING id",
            (name, f"+9199{uuid4().int % 10**8:08d}", ownership_verified),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _new_customer(dsn: str, tenant: UUID, phone: str, *, opt_out: str = "subscribed") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, phone_e164, opt_out_status) "
            "VALUES (%s, %s, %s) RETURNING id",
            (str(tenant), phone, opt_out),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _send(tenant: UUID, customer: UUID, *, send_fn):  # type: ignore[no-untyped-def]
    from orchestrator.agent.tools.send_whatsapp_template import (
        SendWhatsappTemplateInput,
        send_whatsapp_template,
    )
    from orchestrator.graph import get_pool

    payload = SendWhatsappTemplateInput(
        tenant_id=str(tenant),
        customer_id=str(customer),
        template_id="team_weekly_approval",
        language="en",
        template_params={
            "customer_segment": "SMB",
            "campaign_mode": "recovery",
            "projected_recovery_inr": "5000",
        },
        idempotency_key=f"vt301-canary-{uuid4()}",
    )
    return send_whatsapp_template(payload, pool=get_pool(), send_fn=send_fn)


# --- canary ----------------------------------------------------------------


def test_opted_in_customer_send_allowed(substrate):
    from unittest.mock import MagicMock

    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()
    customer = _new_customer(substrate, tenant, phone)
    consent.record_consent(tenant, phone, consent_text_version="wa_inbound_optin_v0")

    send_fn = MagicMock(side_effect=_ok_send_fn)
    out = _send(tenant, customer, send_fn=send_fn)

    assert out.status == "sent", out.error_envelope
    assert out.message_sid is not None
    send_fn.assert_called_once()


def test_no_consent_refused_fail_closed(substrate):
    from unittest.mock import MagicMock

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()
    customer = _new_customer(substrate, tenant, phone)  # NO consent recorded

    send_fn = MagicMock(side_effect=_ok_send_fn)
    out = _send(tenant, customer, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_not_opted_in"
    send_fn.assert_not_called()  # fail-closed: no send


def test_opted_out_refused(substrate):
    from unittest.mock import MagicMock

    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate)
    phone = _synthetic_phone()
    customer = _new_customer(substrate, tenant, phone, opt_out="opted_out")
    # Even WITH a consent row, an opted-out customer is refused (CL-421 check
    # runs first). Belt-and-braces: prove the opt-out path is not weakened.
    consent.record_consent(tenant, phone, consent_text_version="wa_inbound_optin_v0")

    send_fn = MagicMock(side_effect=_ok_send_fn)
    out = _send(tenant, customer, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_opted_out"
    send_fn.assert_not_called()


def test_cross_tenant_consent_does_not_authorize(substrate):
    from unittest.mock import MagicMock

    from orchestrator.privacy import consent

    phone = _synthetic_phone()
    tenant_a = _new_tenant(substrate, name="VT301 tenant A")
    tenant_b = _new_tenant(substrate, name="VT301 tenant B")
    # Consent recorded ONLY under tenant A.
    consent.record_consent(tenant_a, phone, consent_text_version="wa_inbound_optin_v0")
    # Tenant B has its own customer with the same number, no consent under B.
    customer_b = _new_customer(substrate, tenant_b, phone)

    send_fn = MagicMock(side_effect=_ok_send_fn)
    out = _send(tenant_b, customer_b, send_fn=send_fn)

    assert out.status == "unauthorized"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "recipient_not_opted_in"
    send_fn.assert_not_called()
