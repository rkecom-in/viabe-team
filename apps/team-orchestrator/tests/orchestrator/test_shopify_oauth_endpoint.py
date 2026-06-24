"""VT-283 (VT-289-hardened) — Shopify OAuth-install endpoint tests.

TestClient boots the real app (DBOS via lifespan). /setup is POST + INTERNAL_API_SECRET
and returns a JSON authorize URL carrying a single-use VT-289 nonce (NOT the raw
tenant). The callback verifies HMAC (Shopify origin) + claims the nonce (we minted it),
deriving the tenant from the stored record. The exchange is monkeypatched (no network;
CL-422 synthetic only).

# live OAuth-install walk deferred to E2E (Fazal 2026-06-02).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-283 endpoint tests skipped",
)

_SECRET_API = "shopify_secret_test_vt283"
_INTERNAL = "internal_secret_vt283"
_REDIRECT = "https://viabe-team-dev.vercel.app/api/integrations/shopify/oauth/callback"
_SHOP = "merchant-store.myshopify.com"


@pytest.fixture(scope="module")
def app_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["SHOPIFY_API_KEY"] = "cid_test"
    os.environ["SHOPIFY_API_SECRET"] = _SECRET_API
    os.environ["SHOPIFY_OAUTH_REDIRECT_URI"] = _REDIRECT
    os.environ["INTERNAL_API_SECRET"] = _INTERNAL
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
    return hmac.new(_SECRET_API.encode(), message.encode(), hashlib.sha256).hexdigest()


def _mint_via_setup(app_ctx, tenant: str, shop: str = _SHOP) -> str:
    """POST /setup with the internal secret → return the minted state nonce."""
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/shopify/setup",
        json={"tenant_id": tenant, "shop": shop},
        headers={"X-Internal-Secret": _INTERNAL},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    return parse_qs(urlsplit(url).query)["state"][0]


def test_setup_requires_internal_secret(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/shopify/setup",
        json={"tenant_id": _tenant(app_ctx.dsn), "shop": _SHOP},
    )
    assert resp.status_code == 401


def test_setup_returns_authorize_url_with_nonce(app_ctx):
    t = _tenant(app_ctx.dsn)
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/shopify/setup",
        json={"tenant_id": t, "shop": _SHOP},
        headers={"X-Internal-Secret": _INTERNAL},
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    assert url.startswith(f"https://{_SHOP}/admin/oauth/authorize")
    assert t not in url  # VT-289: raw tenant_id never in the URL
    assert "state=" in url


def test_setup_rejects_bad_shop(app_ctx):
    resp = app_ctx.client.post(
        "/api/orchestrator/integrations/shopify/setup",
        json={"tenant_id": _tenant(app_ctx.dsn), "shop": "evil.com"},
        headers={"X-Internal-Secret": _INTERNAL},
    )
    assert resp.status_code == 400


def test_callback_rejects_forged_hmac(app_ctx):
    # forged HMAC is rejected before the nonce is even consulted.
    state = _mint_via_setup(app_ctx, _tenant(app_ctx.dsn))
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback",
        params={"code": "authcode123", "shop": _SHOP, "state": state, "hmac": "deadbeef"},
    )
    assert resp.status_code == 401


def test_callback_rejects_unminted_state(app_ctx):
    # valid HMAC but a state we never minted → claim fails → 401 (the CSRF defense).
    params = {"code": "authcode123", "shop": _SHOP, "state": "forged-never-minted-xyz"}
    params["hmac"] = _sign(params)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp.status_code == 401


def test_callback_success_exchanges_and_persists(app_ctx, monkeypatch):
    import orchestrator.integrations.connectors.shopify as shopify_mod

    def _fake_exchange(shop, client_id, client_secret, code):
        return {"access_token": "shpat_endpoint_ok", "scope": "read_customers,read_orders"}

    monkeypatch.setattr(shopify_mod, "_default_oauth_exchange", _fake_exchange)
    # VT-422: the callback now fires setup_push (webhook registration) on success.
    # Stub it so this persistence test stays network-free; a dedicated test below
    # asserts it IS called.
    monkeypatch.setattr(
        shopify_mod.ShopifyConnector, "setup_push",
        lambda self, tid: {"address": "x", "topics": "orders/create"},
    )

    t = _tenant(app_ctx.dsn)
    state = _mint_via_setup(app_ctx, t)
    params = {"code": "authcode123", "shop": _SHOP, "state": state, "timestamp": "1700000000"}
    params["hmac"] = _sign(params)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "oauth_install"
    assert body["shop_url"] == _SHOP
    # offline token persisted (expires_at NULL), encrypted, merchant shop = minted target.
    with psycopg.connect(app_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT refresh_token_encrypted, expires_at, shop_url FROM tenant_oauth_tokens "
            "WHERE tenant_id=%s AND connector_id='shopify'", (t,)).fetchone()
    assert row is not None
    assert row[0] != "shpat_endpoint_ok"  # encrypted at rest
    assert row[1] is None                  # offline → no expiry
    assert row[2] == _SHOP
    # nonce is single-use: replaying the same callback now fails the claim → 401.
    resp2 = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp2.status_code == 401
    # token reads back without a client_credentials re-grant
    from orchestrator.integrations.connectors.shopify import ShopifyConnector
    token, shop = ShopifyConnector().get_access_token(UUID(t))
    assert token == "shpat_endpoint_ok"


def test_callback_registers_webhooks_via_setup_push(app_ctx, monkeypatch):
    """VT-422 (setup_push flow gap): the OAuth callback MUST call setup_push after
    complete_auth so webhooks register on install (the old callback returned without
    ever registering → orders/create never delivered). Assert setup_push is invoked
    with the resolved tenant, and that registration success surfaces as
    webhooks_registered=true."""
    import orchestrator.integrations.connectors.shopify as shopify_mod

    def _fake_exchange(shop, client_id, client_secret, code):
        return {"access_token": "shpat_webhook_wire", "scope": "read_orders,write_orders"}

    monkeypatch.setattr(shopify_mod, "_default_oauth_exchange", _fake_exchange)

    called: dict[str, str] = {}

    def _fake_setup_push(self, tenant_id):
        called["tenant_id"] = str(tenant_id)
        return {"address": "https://orch/webhook", "topics": "orders/create,orders/paid"}

    monkeypatch.setattr(shopify_mod.ShopifyConnector, "setup_push", _fake_setup_push)

    t = _tenant(app_ctx.dsn)
    state = _mint_via_setup(app_ctx, t)
    params = {"code": "authcode123", "shop": _SHOP, "state": state, "timestamp": "1700000000"}
    params["hmac"] = _sign(params)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp.status_code == 200, resp.text
    # setup_push was called on the install, with the nonce-resolved tenant.
    assert called.get("tenant_id") == t, "callback did not fire setup_push on install"
    assert resp.json()["webhooks_registered"] == "true"


def test_callback_install_survives_webhook_registration_failure(app_ctx, monkeypatch):
    """VT-422: webhook registration is best-effort — a setup_push failure must NOT fail
    the install (the token is already stored, the merchant is connected). The callback
    returns 200 with webhooks_registered=false so the canary can flag it."""
    import orchestrator.integrations.connectors.shopify as shopify_mod

    def _fake_exchange(shop, client_id, client_secret, code):
        return {"access_token": "shpat_wh_fail", "scope": "read_orders"}

    monkeypatch.setattr(shopify_mod, "_default_oauth_exchange", _fake_exchange)

    def _boom(self, tenant_id):
        raise RuntimeError("webhook register failed (e.g. transient 5xx)")

    monkeypatch.setattr(shopify_mod.ShopifyConnector, "setup_push", _boom)

    t = _tenant(app_ctx.dsn)
    state = _mint_via_setup(app_ctx, t)
    params = {"code": "authcode123", "shop": _SHOP, "state": state, "timestamp": "1700000000"}
    params["hmac"] = _sign(params)
    resp = app_ctx.client.get(
        "/api/orchestrator/integrations/shopify/oauth/callback", params=params
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["webhooks_registered"] == "false"
    # the token still persisted despite the webhook-registration failure.
    with psycopg.connect(app_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT 1 FROM tenant_oauth_tokens WHERE tenant_id=%s AND connector_id='shopify'",
            (t,),
        ).fetchone()
    assert row is not None, "install must persist the token even if webhooks fail to register"
