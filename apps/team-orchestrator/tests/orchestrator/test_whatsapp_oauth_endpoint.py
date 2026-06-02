"""VT-286 — WABA Embedded Signup endpoint tests (VT-289-hardened).

TestClient boots the real app. /setup is POST + INTERNAL_API_SECRET → JSON ES URL with
a VT-289 nonce. The callback claims the nonce (forged → 401) and persists via an injected
exchange/provision (monkeypatched; no network).

# live Embedded-Signup walk deferred to E2E (Fazal 2026-06-02).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-286 endpoint tests skipped",
)

_INTERNAL = "internal_secret_vt286"


@pytest.fixture(scope="module")
def app_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _INTERNAL
    os.environ["WA_APP_ID"] = "wa_app_test"
    os.environ["WA_CONFIG_ID"] = "wa_config_test"
    os.environ["WA_REDIRECT_URI"] = (
        "https://viabe-team-dev.vercel.app/api/integrations/whatsapp/embedded-callback"
    )
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt286-ep-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield SimpleNamespace(dsn=dsn, client=client)


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-286 ep test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _mint(app_ctx, tenant: str, display_name: str = "Shop") -> str:
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/whatsapp/setup",
        json={"tenant_id": tenant, "display_name": display_name},
        headers={"X-Internal-Secret": _INTERNAL},
    )
    assert resp.status_code == 200, resp.text
    return parse_qs(urlsplit(resp.json()["embedded_signup_url"]).query)["state"][0]


def test_setup_requires_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/whatsapp/setup",
        json={"tenant_id": _tenant(app_ctx.dsn)},
    )
    assert resp.status_code == 401


def test_setup_returns_es_url_with_nonce(app_ctx):
    t = _tenant(app_ctx.dsn)
    url = app_ctx.client.post(
        "/api/orchestrator/integrations/whatsapp/setup",
        json={"tenant_id": t, "display_name": "Asha Sarees"},
        headers={"X-Internal-Secret": _INTERNAL},
    ).json()["embedded_signup_url"]
    assert "facebook.com" in url and "config_id=wa_config_test" in url
    assert t not in url  # VT-289: no raw tenant in the URL


def test_callback_rejects_forged_state(app_ctx):
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/whatsapp/embedded-callback",
        params={"code": "c", "state": "forged-never-minted"},
    )
    assert resp.status_code == 401


def test_callback_success_persists(app_ctx, monkeypatch):
    import orchestrator.integrations.whatsapp_account as wa

    monkeypatch.setattr(wa, "_default_exchange",
                        lambda code: {"access_token": "wa_tok_ok", "waba_id": "waba_ep"})
    monkeypatch.setattr(wa, "_default_provision",
                        lambda waba_id: {"phone_number": "+919900000002", "phone_number_id": "pn_ep"})

    t = _tenant(app_ctx.dsn)
    state = _mint(app_ctx, t, display_name="Asha Sarees")
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/whatsapp/embedded-callback",
        params={"code": "code_ep", "state": state},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["waba_status"] == "verifying"
    assert body["waba_id"] == "waba_ep"
    # persisted encrypted, display_name carried through the nonce target
    with psycopg.connect(app_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT access_token_encrypted, display_name, status FROM tenant_whatsapp_accounts "
            "WHERE tenant_id=%s", (t,)).fetchone()
    assert row[0] != "wa_tok_ok" and row[1] == "Asha Sarees" and row[2] == "verifying"
    # single-use nonce: replay → 401
    resp2 = app_ctx.client.get(
        "/api/orchestrator/integrations/whatsapp/embedded-callback",
        params={"code": "code_ep", "state": state},
    )
    assert resp2.status_code == 401
    UUID(t)  # sanity
