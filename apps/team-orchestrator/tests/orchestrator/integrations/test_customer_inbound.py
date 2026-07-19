"""VT-287 — inbound-first customer pipeline (Rule #15 canary, real Postgres).

Deterministic handler, injected send (no Twilio). Verifies: intro-once re-send guard
(Cowork's flag), YES→consent, STOP→opt_out, established→reply, and the fail-CLOSED
send-gate (no send unless the WABA is `live`). State is recorded regardless. CL-422
synthetic.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-287 substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt287-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str, *, wa_status: str | None = "live", onboarded: bool = True, ownership_verified: bool = True) -> str:
    """Create a tenant; optionally give it a WABA at `wa_status` (None = no WABA).

    VT-460: the inbound session-send now also passes the shared onboarded (Gate-0) pre-gate
    (``assert_customer_send_allowed``) on top of the WABA-live gate. By default seed the tenant
    fully onboarded (journey-complete + gstin_verified + ≥1 enabled connector + ≥1 customer) so the
    SEND-asserting tests still send; ``onboarded=False`` exercises the new fail-closed path.

    VT-517: ``ownership_verified`` (migration 148) is required by the activation gate for
    'sales_recovery' to send. Defaults True so all SEND-asserting tests pass; pass False for
    tenants the test deliberately expects blocked (WABA-not-live, non-onboarded, etc.).
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, verification_status, ownership_verified) "
            "VALUES ('VT-287 test', 'founding', 'paid_active', %s, %s) RETURNING id",
            ("gstin_verified" if onboarded else "unverified", ownership_verified),
        ).fetchone()[0])
        if wa_status is not None:
            conn.execute(
                "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
                "VALUES (%s, %s, %s)",
                (tid, wa_status, f"+9180{uuid4().int % 10**8:08d}"),
            )
        if onboarded:
            # The activation bar for 'sales_recovery' (activation_registry): journey-complete +
            # gstin_verified (above) + ≥1 enabled+pulled connector + ≥1 customer.
            conn.execute(
                "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
                "VALUES (%s, 'complete', now())",
                (tid,),
            )
            conn.execute(
                "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, "
                "last_status, last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
                (tid, f"conn-{uuid4().hex[:8]}"),
            )
            conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
                "VALUES (%s, 'seed', %s, 'subscribed')",
                (tid, f"+9197{uuid4().int % 10**8:08d}"),
            )
    return tid


def _phone() -> str:
    return f"+9199{uuid4().int % 10**8:08d}"


class _Send:
    def __init__(self):
        self.calls = []

    def __call__(self, body, recipient_phone):
        self.calls.append((body, recipient_phone))
        return f"SM{len(self.calls)}"


def test_first_contact_intro_once(substrate):
    """Two pre-consent inbounds → intro sent ONCE (re-send guard)."""
    from orchestrator.integrations.customer_inbound import handle_customer_inbound

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    send = _Send()
    r1 = handle_customer_inbound(t, phone, "hello?", send_fn=send)
    r2 = handle_customer_inbound(t, phone, "anyone there?", send_fn=send)
    assert r1.action == "intro_sent" and r1.sent is True
    assert r2.action == "intro_suppressed" and r2.sent is False
    assert len(send.calls) == 1  # intro sent exactly once


def test_yes_records_consent(substrate):
    from orchestrator.integrations.customer_inbound import handle_customer_inbound
    from orchestrator.privacy import consent
    from orchestrator.utils.phone_token import hash_phone

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t, phone, "hi", send_fn=_Send())          # intro
    r = handle_customer_inbound(t, phone, "YES", send_fn=_Send())     # opt-in
    assert r.action == "consented"
    assert consent.has_consent(t, hash_phone(phone)) is True


def test_stop_opts_out(substrate):
    from orchestrator.integrations.customer_inbound import handle_customer_inbound
    from orchestrator.privacy import consent
    from orchestrator.utils.phone_token import hash_phone

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t, phone, "hi", send_fn=_Send())
    handle_customer_inbound(t, phone, "YES", send_fn=_Send())
    token = hash_phone(phone)
    assert consent.has_consent(t, token) is True
    r = handle_customer_inbound(t, phone, "STOP", send_fn=_Send())
    assert r.action == "opted_out"
    assert consent.has_consent(t, token) is False


@pytest.mark.parametrize(
    "body",
    [
        "please STOP",  # was MISSED by whole-body-exact (the VT-358 live bug)
        "stop sending me these",
        "please बंद करो",  # Devanagari opt-out mid-sentence
        "band karo bhai",  # Hinglish opt-out
        "ok roko",  # Hinglish
    ],
)
def test_optout_phrase_containment(substrate, body):
    """VT-358: a customer opt-out anywhere in the body (EN/Devanagari/Hinglish) is honored — the
    whole-body-exact gate missed "please STOP" / "please बंद करो" (a DPDP/WhatsApp breach)."""
    from orchestrator.integrations.customer_inbound import handle_customer_inbound
    from orchestrator.privacy import consent
    from orchestrator.utils.phone_token import hash_phone

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t, phone, "hi", send_fn=_Send())
    handle_customer_inbound(t, phone, "YES", send_fn=_Send())
    token = hash_phone(phone)
    assert consent.has_consent(t, token) is True
    r = handle_customer_inbound(t, phone, body, send_fn=_Send())
    assert r.action == "opted_out"
    assert consent.has_consent(t, token) is False


def test_benign_message_not_optout(substrate):
    """VT-358: containment must not over-fire — a benign reply with no opt-out keyword is a reply,
    not an opt-out ("stopwatch"/"nonstop" don't fire; boundary-safe)."""
    from orchestrator.integrations.customer_inbound import handle_customer_inbound

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t, phone, "hi", send_fn=_Send())
    handle_customer_inbound(t, phone, "YES", send_fn=_Send())
    r = handle_customer_inbound(t, phone, "my new stopwatch is great, nonstop fun", send_fn=_Send())
    assert r.action != "opted_out"


def test_established_gets_reply(substrate):
    from orchestrator.integrations.customer_inbound import handle_customer_inbound

    t = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t, phone, "hi", send_fn=_Send())
    handle_customer_inbound(t, phone, "YES", send_fn=_Send())
    send = _Send()
    r = handle_customer_inbound(t, phone, "do you have red sarees?", send_fn=send)
    assert r.action == "reply" and r.sent is True and len(send.calls) == 1


def test_send_gate_fail_closed_when_not_live(substrate):
    """WABA not live → no outbound send, but state (intro marker) still recorded."""
    from orchestrator.integrations.customer_inbound import handle_customer_inbound

    t = _tenant(substrate.dsn, wa_status="verifying", ownership_verified=False)  # not live; blocked by WABA gate
    phone = _phone()
    send = _Send()
    r = handle_customer_inbound(t, phone, "hi", send_fn=send)
    assert r.sent is False and r.action == "gated"
    assert len(send.calls) == 0  # nothing sent

    # STOP still recorded even when not live (never lose an opt-out)
    from orchestrator.integrations.customer_inbound import handle_customer_inbound as h
    from orchestrator.privacy import consent
    from orchestrator.utils.phone_token import hash_phone
    consent.record_consent(t, phone, consent_text_version="v", consent_method="wa_inbound_optin")
    h(t, phone, "STOP", send_fn=_Send())
    assert consent.has_consent(t, hash_phone(phone)) is False


def test_cross_tenant_isolation(substrate):
    from orchestrator.integrations.customer_inbound import handle_customer_inbound

    t_a = _tenant(substrate.dsn, wa_status="live")
    t_b = _tenant(substrate.dsn, wa_status="live")
    phone = _phone()
    handle_customer_inbound(t_a, phone, "hi", send_fn=_Send())   # intro for A
    # same phone, tenant B → first-contact for B (A's conversation invisible via RLS)
    r = handle_customer_inbound(t_b, phone, "hi", send_fn=_Send())
    assert r.action == "intro_sent"
    UUID(t_b)
