"""VT-283 — Shopify OAuth-install endpoint (setup + callback) tests.

TestClient boots the real FastAPI app (DBOS launched via lifespan). The
security-critical callback gates — forged-HMAC rejection and state validation —
run BEFORE any code-exchange and need no network. The success path monkeypatches
the connector's OAuth exchange (no real Shopify call; CL-422 synthetic only).

# live OAuth-install walk deferred to E2E (Fazal 2026-06-02).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-283 endpoint tests skipped",
)

_SECRET = "secret_test_vt283"
_REDIRECT = "https://viabe-team-dev.vercel.app/api/integrations/shopify/oauth/callback"


@pytest.fixture(scope="module")
def app_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["SHOPIFY_API_KEY"] = "cid_test"
    os.environ["SHOPIFY_API_SECRET"] = _SECRET
    os.environ["SHOPIFY_OAUTH_REDIRECT_URI"] = _REDIRECT
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt283-test-salt")
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
            "VALUES ('VT-283 endpoint test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _sign(params: dict[str, str]) -> str:
    message = "&".join(sorted(f"{k}={v}" for k, v in params.items()))
    return hmac.new(_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_setup_redirects_to_consent(app_ctx):
    t = _tenant(app_ctx.dsn)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/setup",
        params={"tenant_id": t, "shop": "merchant-store.myshopify.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://merchant-store.myshopify.com/admin/oauth/authorize")
    assert f"state={t}" in loc


def test_setup_rejects_bad_shop(app_ctx):
    t = _tenant(app_ctx.dsn)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/setup",
        params={"tenant_id": t, "shop": "evil.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_callback_rejects_forged_hmac(app_ctx):
    t = _tenant(app_ctx.dsn)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback",
        params={
            "code": "authcode123",
            "shop": "merchant-store.myshopify.com",
            "state": t,
            "hmac": "deadbeef",  # forged
        },
    )
    assert resp.status_code == 401


def test_callback_rejects_bad_state(app_ctx):
    # state is validated before HMAC; a non-UUID state is a 400 regardless.
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback",
        params={
            "code": "authcode123",
            "shop": "merchant-store.myshopify.com",
            "state": "not-a-uuid",
            "hmac": "whatever",
        },
    )
    assert resp.status_code == 400


def test_callback_success_exchanges_and_persists(app_ctx, monkeypatch):
    import orchestrator.integrations.connectors.shopify as shopify_mod

    def _fake_exchange(shop, client_id, client_secret, code):
        return {"access_token": "shpat_endpoint_ok", "scope": "read_customers,read_orders"}

    monkeypatch.setattr(shopify_mod, "_default_oauth_exchange", _fake_exchange)

    t = _tenant(app_ctx.dsn)
    params = {
        "code": "authcode123",
        "shop": "merchant-store.myshopify.com",
        "state": t,
        "timestamp": "1700000000",
    }
    params["hmac"] = _sign(params)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "oauth_install"
    assert body["shop_url"] == "merchant-store.myshopify.com"
    # offline token persisted (expires_at NULL), encrypted, merchant shop.
    with psycopg.connect(app_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT refresh_token_encrypted, expires_at, shop_url FROM tenant_oauth_tokens "
            "WHERE tenant_id=%s AND connector_id='shopify'", (t,)).fetchone()
    assert row is not None
    assert row[0] != "shpat_endpoint_ok"  # encrypted at rest
    assert row[1] is None                  # offline → no expiry
    assert row[2] == "merchant-store.myshopify.com"
    # the connector reads it back without a client_credentials re-grant
    from orchestrator.integrations.connectors.shopify import ShopifyConnector
    token, shop = ShopifyConnector().get_access_token(UUID(t))
    assert token == "shpat_endpoint_ok"
