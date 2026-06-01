"""VT-208 — Shopify client_credentials grant rework.

PURE: zero-paste start_auth, config-error, grant rejection (shop_not_permitted),
webhook HMAC verify, attribution parse. DB (real Postgres, no mock cursors): grant
+ encrypted persist, token cache (no re-grant), proactive refresh on near-expiry.
The grant HTTP is FAKED (injectable grant_fn); the LIVE real-store walk is
Fazal-gated. CL-422 synthetic only.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.connectors.shopify import (  # noqa: E402
    AuthValidationError,
    ShopifyConfigError,
    ShopifyConnector,
)

_ENV = {"SHOPIFY_API_KEY": "cid_test", "SHOPIFY_API_SECRET": "secret_test",
        "SHOPIFY_STORE_DOMAIN": "kk4xva-di.myshopify.com"}


class _Grant:
    """Counting fake client_credentials grant."""

    def __init__(self, scope="read_customers,read_orders,read_products", expires_in=86399):
        self.calls = 0
        self.scope = scope
        self.expires_in = expires_in

    def __call__(self, store, client_id, client_secret):
        self.calls += 1
        self.last = (store, client_id, client_secret)
        return {"access_token": f"shpat_test_{self.calls}",
                "scope": self.scope, "expires_in": self.expires_in}


# --- PURE ---------------------------------------------------------------------

def test_start_auth_zero_paste():
    env = ShopifyConnector(grant_fn=_Grant()).start_auth(uuid4())
    assert env["prompt_kind"] == "none"  # CL-421: no token to paste
    assert env["next_action"] == "client_credentials_connect"
    assert "token" not in str(env.get("walkthrough", "")).lower()


def test_config_error_when_env_absent(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ShopifyConfigError):
        ShopifyConnector(grant_fn=_Grant()).complete_auth(uuid4())


def test_grant_rejection_propagates(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)

    def _boom(store, cid, secret):
        raise AuthValidationError("HTTP 401 shop_not_permitted")

    with pytest.raises(AuthValidationError, match="shop_not_permitted"):
        ShopifyConnector(grant_fn=_boom).complete_auth(uuid4())


def test_verify_push_signature():
    import base64
    import hashlib
    import hmac

    body = b'{"id":1}'
    secret = "whsec_test"
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert ShopifyConnector.verify_push_signature(body, {"x-shopify-hmac-sha256": sig}, secret)
    assert not ShopifyConnector.verify_push_signature(body, {"x-shopify-hmac-sha256": "bad"}, secret)


def test_parse_push_payload_attribution():
    import json
    body = json.dumps({"customer": {"phone": "+919876500001", "first_name": "Asha"},
                       "total_price": "500.00", "created_at": "2026-06-01T00:00:00Z"}).encode()
    rows = ShopifyConnector.parse_push_payload(body)
    assert len(rows) == 1 and rows[0]["order_amount"] == "500.00"
    assert rows[0]["acquired_via"] == "shopify"
    assert ShopifyConnector.parse_push_payload(b'{"total_price":"9"}') == []  # no phone/email


# --- DB (real Postgres) -------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — shopify DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
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


@pytest.fixture()
def _shopify_env_set(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-208 shopify test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


@_DB
def test_grant_persists_encrypted_token(db_ctx, _shopify_env_set):
    from uuid import UUID

    grant = _Grant()
    conn = ShopifyConnector(grant_fn=grant)
    t = _tenant(db_ctx.dsn)
    out = conn.complete_auth(UUID(t))
    assert out["success"] and out["shop_url"] == "kk4xva-di.myshopify.com"
    assert grant.last == ("kk4xva-di.myshopify.com", "cid_test", "secret_test")
    token, shop = conn.get_access_token(UUID(t))
    assert token == "shpat_test_1" and shop == "kk4xva-di.myshopify.com"
    # stored encrypted (not plaintext) + expires_at ~24h out
    with psycopg.connect(db_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT refresh_token_encrypted, expires_at, scopes FROM tenant_oauth_tokens "
            "WHERE tenant_id=%s AND connector_id='shopify'", (t,)).fetchone()
    assert row[0] != "shpat_test_1"  # encrypted at rest
    assert (row[1] - datetime.now(UTC)).total_seconds() > 86000  # ~24h TTL
    assert "read_orders" in row[2]


@_DB
def test_token_cached_no_regrant(db_ctx, _shopify_env_set):
    from uuid import UUID

    grant = _Grant()
    conn = ShopifyConnector(grant_fn=grant)
    t = _tenant(db_ctx.dsn)
    conn.complete_auth(UUID(t))           # grant #1
    conn.get_access_token(UUID(t))        # cached — no grant
    conn.get_access_token(UUID(t))        # cached — no grant
    assert grant.calls == 1


@_DB
def test_proactive_refresh_on_near_expiry(db_ctx, _shopify_env_set):
    from uuid import UUID

    grant = _Grant()
    conn = ShopifyConnector(grant_fn=grant)
    t = _tenant(db_ctx.dsn)
    conn.complete_auth(UUID(t))           # grant #1
    # Force the token near/past expiry → next get must re-grant.
    with psycopg.connect(db_ctx.dsn, autocommit=True) as c:
        c.execute("UPDATE tenant_oauth_tokens SET expires_at = now() - interval '1 hour' "
                  "WHERE tenant_id=%s AND connector_id='shopify'", (t,))
    token, _ = conn.get_access_token(UUID(t))
    assert grant.calls == 2 and token == "shpat_test_2"  # re-granted
