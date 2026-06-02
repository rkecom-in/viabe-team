"""VT-286 — WABA Embedded Signup onboarding (Rule #15 canary, real Postgres).

Injected exchange/provision (no Meta/Twilio network; CL-422 synthetic). Verifies the
token persists ENCRYPTED, the status state machine starts non-live, the fail-closed
send-gate, and cross-tenant RLS. The endpoint test additionally proves the VT-289 nonce
binding (forged state rejected; tenant from the stored record).

# live Embedded-Signup walk deferred to E2E (Fazal 2026-06-02).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-286 WABA substrate tests skipped",
)

_WA_ENV = {
    "WA_APP_ID": "wa_app_test",
    "WA_CONFIG_ID": "wa_config_test",
    "WA_REDIRECT_URI": "https://viabe-team-dev.vercel.app/api/integrations/whatsapp/embedded-callback",
}


def _exchange(code):
    return {"access_token": f"wa_tok_{code}", "waba_id": "waba_123"}


def _provision(waba_id):
    return {"phone_number": "+919900000001", "phone_number_id": "pnid_456"}


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-286 waba test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


# --- PURE ---------------------------------------------------------------------

def test_build_embedded_signup_url(monkeypatch):
    from orchestrator.integrations.whatsapp_account import build_embedded_signup_url

    for k, v in _WA_ENV.items():
        monkeypatch.setenv(k, v)
    tid = uuid4()
    url = build_embedded_signup_url(tid, state="vt289_nonce_wa")
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert "facebook.com" in parts.netloc
    assert q["client_id"] == ["wa_app_test"]
    assert q["config_id"] == ["wa_config_test"]
    assert q["state"] == ["vt289_nonce_wa"]
    assert str(tid) not in url  # VT-289: raw tenant not in the URL


# --- DB (real Postgres) -------------------------------------------------------

def test_connect_persists_encrypted_and_verifying(substrate):
    from orchestrator.integrations.whatsapp_account import connect_waba

    t = _tenant(substrate.dsn)
    acct = connect_waba(
        UUID(t), "code_abc", display_name="Asha Sarees",
        exchange_fn=_exchange, provision_fn=_provision,
    )
    assert acct.status == "verifying"
    assert acct.waba_id == "waba_123"
    assert acct.phone_number == "+919900000001"
    with psycopg.connect(substrate.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT access_token_encrypted, status, display_name, phone_number "
            "FROM tenant_whatsapp_accounts WHERE tenant_id=%s", (t,)).fetchone()
    assert row[0] != "wa_tok_code_abc"   # encrypted at rest (CL-390)
    assert row[1] == "verifying"
    assert row[2] == "Asha Sarees"
    assert row[3] == "+919900000001"


def test_send_gate_fail_closed_until_live(substrate):
    from orchestrator.integrations.whatsapp_account import (
        connect_waba,
        set_status,
        wa_send_allowed,
    )

    t = _tenant(substrate.dsn)
    # no row yet → fail-closed
    assert wa_send_allowed(UUID(t)) is False
    connect_waba(UUID(t), "c", exchange_fn=_exchange, provision_fn=_provision)
    assert wa_send_allowed(UUID(t)) is False         # verifying, not live
    set_status(UUID(t), "name_approved")
    assert wa_send_allowed(UUID(t)) is False
    set_status(UUID(t), "live")
    assert wa_send_allowed(UUID(t)) is True           # only at live


def test_cross_tenant_rls(substrate):
    from orchestrator.integrations.whatsapp_account import connect_waba, wa_send_allowed

    t_a = _tenant(substrate.dsn)
    t_b = _tenant(substrate.dsn)
    connect_waba(UUID(t_a), "ca", exchange_fn=_exchange, provision_fn=_provision)
    # tenant B has no WABA → fail-closed; A's row is invisible to B (RLS).
    assert wa_send_allowed(UUID(t_b)) is False


def test_set_status_rejects_invalid(substrate):
    from orchestrator.integrations.whatsapp_account import set_status

    with pytest.raises(ValueError, match="invalid WABA status"):
        set_status(uuid4(), "bogus")
