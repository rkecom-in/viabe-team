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


# === VT-283 — OWNER-FACING OAuth managed-install =============================

from orchestrator.integrations.connectors.shopify import (  # noqa: E402
    ShopDomainError,
    verify_oauth_hmac,
)

_OAUTH_ENV = {
    **_ENV,
    "SHOPIFY_OAUTH_REDIRECT_URI": (
        "https://viabe-team-dev.vercel.app/api/integrations/shopify/oauth/callback"
    ),
}


class _Exchange:
    """Counting fake OAuth authorization-code exchange (returns an offline token)."""

    def __init__(self, scope="read_customers,read_orders,read_products"):
        self.calls = 0
        self.scope = scope

    def __call__(self, shop, client_id, client_secret, code):
        self.calls += 1
        self.last = (shop, client_id, client_secret, code)
        return {"access_token": f"shpat_oauth_{self.calls}", "scope": self.scope}


# --- PURE ---------------------------------------------------------------------

def test_build_oauth_install_url(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    tid = uuid4()
    # VT-289: state is the minted nonce, NOT the raw tenant_id.
    nonce = "vt289_nonce_shopify_xyz"
    url = ShopifyConnector().build_oauth_install_url(
        tid, "merchant-store.myshopify.com", state=nonce
    )
    parts = urlsplit(url)
    assert parts.netloc == "merchant-store.myshopify.com"
    assert parts.path == "/admin/oauth/authorize"
    q = parse_qs(parts.query)
    assert q["client_id"] == ["cid_test"]
    assert q["state"] == [nonce]
    assert str(tid) not in url  # VT-289: raw tenant_id must NOT be in the URL
    assert q["redirect_uri"] == [_OAUTH_ENV["SHOPIFY_OAUTH_REDIRECT_URI"]]
    # scopes present, comma-joined; offline (no grant_options[]=per-user).
    assert "read_customers" in q["scope"][0] and "read_orders" in q["scope"][0]
    assert "grant_options" not in q


@pytest.mark.parametrize("bad", [
    "evil.com",
    "merchant.myshopify.com.evil.com",
    "https://merchant.myshopify.com",
    "merchant.myshopify.com/admin",
    "MERCHANT.myshopify.com/../x",
    "",
    "merchant.myshopify.com:443",
])
def test_build_oauth_install_url_rejects_bad_shop(monkeypatch, bad):
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ShopDomainError):
        ShopifyConnector().build_oauth_install_url(uuid4(), bad, state="nonce")


def test_verify_oauth_hmac():
    import hashlib
    import hmac as _hmac

    secret = "secret_test"
    params = {
        "code": "authcode123",
        "shop": "merchant-store.myshopify.com",
        "state": "tenant-uuid",
        "timestamp": "1700000000",
    }
    message = "&".join(sorted(f"{k}={v}" for k, v in params.items()))
    params["hmac"] = _hmac.new(
        secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    assert verify_oauth_hmac(params, secret) is True
    # tamper a signed field → mismatch
    assert verify_oauth_hmac({**params, "code": "tampered"}, secret) is False
    # missing hmac → fail-closed
    assert verify_oauth_hmac({"code": "x"}, secret) is False
    # wrong secret → mismatch
    assert verify_oauth_hmac(params, "wrong_secret") is False


# --- DB (real Postgres) -------------------------------------------------------

@pytest.fixture()
def _shopify_oauth_env_set(monkeypatch):
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)


@_DB
def test_oauth_install_persists_offline_token(db_ctx, _shopify_oauth_env_set):
    from uuid import UUID

    ex = _Exchange()
    conn = ShopifyConnector(exchange_fn=ex)
    t = _tenant(db_ctx.dsn)
    out = conn.complete_auth(
        UUID(t), {"code": "authcode123", "shop": "merchant-store.myshopify.com"}
    )
    assert out["mode"] == "oauth_install"
    assert out["shop_url"] == "merchant-store.myshopify.com"
    assert ex.last == (
        "merchant-store.myshopify.com", "cid_test", "secret_test", "authcode123"
    )
    # offline token: stored encrypted, expires_at NULL, merchant shop persisted.
    with psycopg.connect(db_ctx.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT refresh_token_encrypted, expires_at, shop_url, scopes "
            "FROM tenant_oauth_tokens WHERE tenant_id=%s AND connector_id='shopify'",
            (t,)).fetchone()
    assert row[0] != "shpat_oauth_1"          # encrypted at rest
    assert row[1] is None                     # offline → no expiry
    assert row[2] == "merchant-store.myshopify.com"
    assert "read_orders" in row[3]
    # usable + decrypts back
    token, shop = conn.get_access_token(UUID(t))
    assert token == "shpat_oauth_1" and shop == "merchant-store.myshopify.com"


@_DB
def test_oauth_offline_token_never_regranted(db_ctx, _shopify_oauth_env_set):
    """An offline OAuth token (expires_at NULL) must NOT trigger a
    client_credentials re-grant — that would 403 on a real merchant store."""
    from uuid import UUID

    grant = _Grant()
    ex = _Exchange()
    conn = ShopifyConnector(grant_fn=grant, exchange_fn=ex)
    t = _tenant(db_ctx.dsn)
    conn.complete_auth(
        UUID(t), {"code": "authcode123", "shop": "merchant-store.myshopify.com"}
    )
    conn.get_access_token(UUID(t))
    conn.get_access_token(UUID(t))
    assert ex.calls == 1     # exchanged once at install
    assert grant.calls == 0  # NEVER fell back to client_credentials


@_DB
def test_complete_auth_mode_selection_client_credentials(db_ctx, _shopify_oauth_env_set):
    """No code → client_credentials path (the dev/own-store mode)."""
    from uuid import UUID

    grant = _Grant()
    ex = _Exchange()
    conn = ShopifyConnector(grant_fn=grant, exchange_fn=ex)
    t = _tenant(db_ctx.dsn)
    out = conn.complete_auth(UUID(t))                 # empty payload
    assert out["shop_url"] == "kk4xva-di.myshopify.com"  # the env store, not a merchant
    assert grant.calls == 1 and ex.calls == 0


def test_complete_auth_oauth_rejects_bad_shop(monkeypatch):
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ShopDomainError):
        ShopifyConnector(exchange_fn=_Exchange()).complete_auth(
            uuid4(), {"code": "authcode123", "shop": "evil.com"}
        )


def test_complete_auth_oauth_code_without_shop_raises(monkeypatch):
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError, match="shop"):
        ShopifyConnector(exchange_fn=_Exchange()).complete_auth(
            uuid4(), {"code": "authcode123"}
        )
