"""VT-417 PR-1 — real Shopify ingestion connector tests + the deterministic
webhook-sim canary (Rule #15: a real write end-to-end against a throwaway PG16).

PURE: the order→CanonicalRow mapper (identity, total→paise, date, currency
guard), the ACQUIRED_VIA enum tags, HMAC accept/reject.
DB: ingest_customer_rows lands a real customers row + a real `sale`
customer_ledger_entries row; webhook-sim canary (valid HMAC → handler →
asserted rows); webhook re-delivery idempotency; ambiguous → no ledger; the
consent AND-gate (option A) — a Shopify-ingested customer is NOT a lapsed
candidate until a separate record_consent lands.

Real Postgres (DATABASE_URL), no mock cursors (the VT-263 / Cowork bar).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.connectors.shopify import (  # noqa: E402
    _normalize_e164,
    _total_price_to_paise,
    shopify_order_to_canonical,
)
from orchestrator.integrations.dedup_merge import ACQUIRED_VIA  # noqa: E402
from orchestrator.integrations.ingest import (  # noqa: E402
    CanonicalRow,
    SaleLine,
)


# --- PURE: enum tags ----------------------------------------------------------

def test_vt417_enum_tags_present():
    # The writers RAISE on an unknown tag; the inbound lineage needs these three.
    assert {"shopify", "google_sheet", "drive_sheet"} <= ACQUIRED_VIA


# --- PURE: phone normalization ------------------------------------------------

def test_normalize_e164():
    assert _normalize_e164("+919876500001") == "+919876500001"   # already E.164 IN
    assert _normalize_e164("9876500001") == "+919876500001"      # bare 10-digit IN
    assert _normalize_e164("09876500001") == "+919876500001"     # leading-0 IN
    assert _normalize_e164("+14155550100") == "+14155550100"     # intl, trusted
    assert _normalize_e164("") is None
    assert _normalize_e164(None) is None
    assert _normalize_e164("12345") is None                      # ambiguous → None


# --- PURE: total_price → paise ------------------------------------------------

def test_total_price_to_paise():
    assert _total_price_to_paise("499.00") == 49900
    assert _total_price_to_paise("0.99") == 99
    assert _total_price_to_paise("1000") == 100000
    assert _total_price_to_paise(Decimal("12.50")) == 1250
    assert _total_price_to_paise(None) is None
    assert _total_price_to_paise("not-a-number") is None
    assert _total_price_to_paise("-5.00") is None                # negative → None


# --- PURE: the order → CanonicalRow mapper ------------------------------------

def _order(phone="+919876500001", total="499.00", currency="INR",
           created="2026-06-01T10:30:00Z", first="Asha", last="K",
           email=None, address=None):
    payload = {
        "customer": {"phone": phone, "first_name": first, "last_name": last,
                     "email": email},
        "total_price": total,
        "currency": currency,
        "created_at": created,
    }
    if address is not None:
        payload["shipping_address"] = address
    return payload


def test_mapper_inr_order_full_row():
    out = shopify_order_to_canonical(_order())
    assert out.skipped_non_inr is False
    row = out.row
    assert row is not None
    assert row.phone_e164 == "+919876500001"
    assert row.display_name == "Asha K"
    assert row.consent is None                                   # option A
    assert len(row.sales) == 1
    assert row.sales[0].amount_paise == 49900
    assert row.sales[0].entry_date == date(2026, 6, 1)
    assert row.sales[0].confidence == 1.0


def test_mapper_drops_address_and_lineitems():
    # PII boundary (§3): address + line_items must NOT survive into CanonicalRow.
    payload = _order(address={"address1": "12 MG Road", "phone": "+919000000009"})
    payload["line_items"] = [{"title": "Widget", "price": "499.00"}]
    row = shopify_order_to_canonical(payload).row
    assert row is not None
    # CanonicalRow's only fields are identity + sale magnitude — no address attr.
    assert set(row.model_dump().keys()) == {
        "phone_e164", "email", "display_name", "sales", "consent"
    }


def test_mapper_non_inr_keeps_identity_skips_sale():
    out = shopify_order_to_canonical(_order(currency="USD", total="20.00"))
    assert out.skipped_non_inr is True
    assert out.row is not None
    assert out.row.phone_e164 == "+919876500001"
    assert out.row.sales == ()                                   # no FX → no sale


def test_mapper_email_anchor_no_phone():
    out = shopify_order_to_canonical(
        _order(phone=None, email="Buyer@Example.COM", first="", last="")
    )
    row = out.row
    assert row is not None
    assert row.phone_e164 is None
    assert row.email == "buyer@example.com"                      # lowercased


def test_mapper_no_anchor_returns_none():
    out = shopify_order_to_canonical(
        {"total_price": "100.00", "currency": "INR",
         "created_at": "2026-06-01T00:00:00Z"}
    )
    assert out.row is None


def test_mapper_missing_total_identity_only():
    out = shopify_order_to_canonical(
        {"customer": {"phone": "+919876500002", "first_name": "Bina"},
         "currency": "INR", "created_at": "2026-06-01T00:00:00Z"}
    )
    assert out.row is not None
    assert out.row.sales == ()                                   # no amount → no sale


# --- PURE: HMAC verify --------------------------------------------------------

def test_hmac_accept_and_reject():
    import base64
    import hashlib
    import hmac

    from orchestrator.integrations.connectors.shopify import ShopifyConnector

    body = b'{"id":42}'
    secret = "whsec_vt417"
    sig = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    assert ShopifyConnector.verify_push_signature(
        body, {"x-shopify-hmac-sha256": sig}, secret
    )
    assert not ShopifyConnector.verify_push_signature(
        body, {"x-shopify-hmac-sha256": "tampered"}, secret
    )


# --- DB + CANARY (real Postgres) ----------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-417 ingest DB/canary tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    if not os.environ.get("TEAM_PHONE_HASH_SALT"):
        os.environ["TEAM_PHONE_HASH_SALT"] = "vt417-canary-salt"
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
            "VALUES ('VT-417 shopify ingest test', 'founding', 'onboarding') "
            "RETURNING id"
        ).fetchone()[0])


def _uniq_phone() -> str:
    return "+9190" + uuid4().int.__str__()[:8]


@_DB
def test_ingest_lands_customer_and_sale(db_ctx):
    """The PROOF: a parsed Shopify order → a real customers row + a real `sale`
    customer_ledger_entries row with the right amount/date/acquired_via."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    row = CanonicalRow(
        phone_e164=phone, display_name="Asha K",
        sales=(SaleLine(amount_paise=49900, entry_date=date(2026, 6, 1)),),
    )
    summary = ingest_customer_rows(tenant, [row], acquired_via="shopify")
    assert (summary.committed, summary.sales_written) == (1, 1)

    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id, display_name, phone_e164, acquired_via FROM customers "
            "WHERE tenant_id = %s AND phone_e164 = %s", (tenant, phone)
        ).fetchone()
        assert cust is not None
        assert cust["display_name"] == "Asha K"
        assert "shopify" in (cust["acquired_via"] or [])
        led = conn.execute(
            "SELECT amount_paise, entry_type, entry_date, acquired_via "
            "FROM customer_ledger_entries WHERE tenant_id = %s AND customer_id = %s",
            (tenant, cust["id"])
        ).fetchall()
    assert len(led) == 1
    assert led[0]["amount_paise"] == 49900
    assert led[0]["entry_type"] == "sale"
    assert led[0]["entry_date"] == date(2026, 6, 1)
    assert led[0]["acquired_via"] == "shopify"


@_DB
def test_webhook_sim_canary_full_write(db_ctx, monkeypatch):
    """CANARY — VT-422 GAP-2 real-delivery shape. Construct a realistic orders/create
    payload, compute the VALID HMAC with the APP client_secret (SHOPIFY_API_SECRET) —
    NOT a per-tenant push_secret — deliver with the X-Shopify-Shop-Domain header (the
    only tenant linkage on an app-delivered webhook), invoke the handler, and ASSERT a
    real customers row + a real `sale` ledger row land. The end-to-end inbound proof
    (Rule #15) on the real public-app model."""
    import asyncio
    import base64
    import hashlib
    import hmac
    import json

    from orchestrator.api.shopify_webhook import shopify_webhook
    from orchestrator.db import tenant_connection

    tenant = _tenant(db_ctx.dsn)
    # Unique shop per test — the GAP-2 resolver rejects a shop_url shared by >1 tenant
    # (the ambiguity guard), and the module-scoped DB accumulates rows across tests.
    shop_domain = f"canary-{uuid4().hex[:10]}.myshopify.com"
    # VT-422: the webhook is verified against the APP secret, not push_secret.
    app_secret = "shpss_vt422_app_secret_canary"  # gitleaks:allow — fake test app secret for the HMAC canary, not a real credential
    monkeypatch.setenv("SHOPIFY_API_SECRET", app_secret)
    with psycopg.connect(db_ctx.dsn, autocommit=True) as conn:
        # push_secret is now vestigial for the webhook path — the row exists with a
        # shop_url (the GAP-2 tenant-resolution key) and the app secret verifies.
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes, "
            " push_secret, shop_url) "
            "VALUES (%s, 'shopify', 'enc', "
            "ARRAY['read_orders','write_orders'], 'whsec_vestigial', %s)",
            (tenant, shop_domain),
        )

    phone = _uniq_phone()
    order = {
        "id": 123456789,
        "customer": {"phone": phone, "first_name": "Canary", "last_name": "Test",
                     "email": "canary@example.com"},
        "shipping_address": {"address1": "DROP ME", "phone": phone},
        "line_items": [{"title": "DROP ME", "price": "799.00"}],
        "total_price": "799.00",
        "currency": "INR",
        "created_at": "2026-06-10T08:15:00Z",
    }
    body = json.dumps(order).encode()
    # VT-422: sign with the APP secret (base64 over the raw body) — the real model.
    sig = base64.b64encode(
        hmac.new(app_secret.encode(), body, hashlib.sha256).digest()
    ).decode()

    class _Req:
        headers = {"x-shopify-hmac-sha256": sig}

        async def body(self):
            return body

    out = asyncio.run(
        shopify_webhook(
            _Req(),  # type: ignore[arg-type]
            x_shopify_shop_domain=shop_domain,
            x_shopify_topic="orders/create",
            x_shopify_hmac_sha256=sig,
        )
    )
    assert out["status"] == "ok"
    assert out["rows_committed"] == 1
    assert out["sales_written"] == 1

    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (tenant, phone)
        ).fetchone()
        assert cust is not None, "canary: no customers row landed"
        led = conn.execute(
            "SELECT amount_paise, entry_type, acquired_via FROM "
            "customer_ledger_entries WHERE tenant_id = %s AND customer_id = %s",
            (tenant, cust["id"])
        ).fetchall()
        # PII boundary: address/line-items never persisted (no such columns).
    assert len(led) == 1
    assert led[0]["amount_paise"] == 79900       # 799.00 INR → paise
    assert led[0]["entry_type"] == "sale"
    assert led[0]["acquired_via"] == "shopify"


@_DB
def test_webhook_redelivery_idempotent(db_ctx):
    """Shopify retries the same order → NO duplicate customer, NO double-count
    ledger (entry_key ON CONFLICT + the customers unique phone index)."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    row = CanonicalRow(
        phone_e164=phone, display_name="Retry Cust",
        sales=(SaleLine(amount_paise=12300, entry_date=date(2026, 6, 2)),),
    )
    s1 = ingest_customer_rows(tenant, [row], acquired_via="shopify")
    s2 = ingest_customer_rows(tenant, [row], acquired_via="shopify")
    assert s1.sales_written == 1
    assert (s2.sales_written, s2.sales_skipped_duplicate) == (0, 1)

    with tenant_connection(tenant) as conn:
        ncust = conn.execute(
            "SELECT count(*) AS n FROM customers WHERE tenant_id = %s AND "
            "phone_e164 = %s", (tenant, phone)
        ).fetchone()["n"]
        nled = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries WHERE "
            "tenant_id = %s", (tenant,)
        ).fetchone()["n"]
    assert ncust == 1, "re-delivery duplicated the customer"
    assert nled == 1, "re-delivery double-counted the sale"


@_DB
def test_consent_and_gate_option_a(db_ctx):
    """Option A (§2.4): Shopify ingestion writes NO record_of_consent. The
    Sales-Recovery detector AND-gates on an active consent row, so the
    Shopify-ingested customer is NOT a lapsed candidate until they opt in
    separately — fail-closed, DPDP-clean."""
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.ingest import ingest_customer_rows

    tenant = _tenant(db_ctx.dsn)
    phone = _uniq_phone()
    ingest_customer_rows(
        tenant,
        [CanonicalRow(
            phone_e164=phone, display_name="No Consent",
            sales=(SaleLine(amount_paise=99900, entry_date=date(2025, 1, 1)),),
        )],
        acquired_via="shopify",
    )
    # No consent row was written by ingestion.
    with tenant_connection(tenant) as conn:
        nconsent = conn.execute(
            "SELECT count(*) AS n FROM record_of_consent WHERE tenant_id = %s",
            (tenant,)
        ).fetchone()["n"]
    assert nconsent == 0, "Shopify ingestion must NOT write consent (option A)"


# --- VT-422 GAP-2: real app-delivery auth (app-secret HMAC + shop→tenant) ----


def _make_req(body: bytes, sig: str):
    """A minimal stand-in for the FastAPI Request the handler consumes."""

    class _Req:
        headers = {"x-shopify-hmac-sha256": sig}

        async def body(self):
            return body

    return _Req()


def test_vt422_webhook_rejects_bad_app_secret_signature(monkeypatch):
    """GAP-2: a delivery whose HMAC does NOT verify against the APP client_secret is
    rejected (403) BEFORE any tenant lookup — pure, no DB. Proves the verify is keyed
    on SHOPIFY_API_SECRET (the app secret), and a forged/tampered body never drives a
    DB resolution."""
    import asyncio

    from fastapi import HTTPException

    from orchestrator.api.shopify_webhook import shopify_webhook

    monkeypatch.setenv("SHOPIFY_API_SECRET", "shpss_app_secret_test")  # gitleaks:allow — fake test app secret
    body = b'{"id":1}'
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            shopify_webhook(
                _make_req(body, "not-a-valid-base64-hmac"),  # type: ignore[arg-type]
                x_shopify_shop_domain="kk4xva-di.myshopify.com",
                x_shopify_topic="orders/create",
                x_shopify_hmac_sha256="not-a-valid-base64-hmac",
            )
        )
    assert ei.value.status_code == 403


def test_vt422_webhook_requires_shop_domain_header(monkeypatch):
    """GAP-2: the X-Shopify-Shop-Domain header is the only tenant linkage on an
    app-delivered webhook; absent → 400 (pure, no DB)."""
    import asyncio

    from fastapi import HTTPException

    from orchestrator.api.shopify_webhook import shopify_webhook

    monkeypatch.setenv("SHOPIFY_API_SECRET", "shpss_app_secret_test")  # gitleaks:allow — fake test app secret
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            shopify_webhook(
                _make_req(b"{}", "sig"),  # type: ignore[arg-type]
                x_shopify_shop_domain="",
                x_shopify_topic="orders/create",
                x_shopify_hmac_sha256="sig",
            )
        )
    assert ei.value.status_code == 400


@_DB
def test_vt422_webhook_resolves_tenant_from_shop_domain(db_ctx, monkeypatch):
    """GAP-2: with a VALID app-secret HMAC, the handler resolves the tenant SOLELY
    from X-Shopify-Shop-Domain → tenant_oauth_tokens.shop_url (no X-Viabe-Tenant
    header at all) and drives the ingest. Real PG (the resolution is a DB lookup)."""
    import asyncio
    import base64
    import hashlib
    import hmac
    import json

    from orchestrator.api.shopify_webhook import shopify_webhook
    from orchestrator.db import tenant_connection

    tenant = _tenant(db_ctx.dsn)
    # Unique shop per test (the GAP-2 ambiguity guard rejects a shop shared by >1 tenant;
    # the module-scoped DB accumulates rows across tests).
    shop_domain = f"resolve-{uuid4().hex[:10]}.myshopify.com"
    app_secret = "shpss_resolve_test"  # gitleaks:allow — fake test app secret
    monkeypatch.setenv("SHOPIFY_API_SECRET", app_secret)
    with psycopg.connect(db_ctx.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes, "
            " push_secret, shop_url) "
            "VALUES (%s, 'shopify', 'enc', ARRAY['read_orders'], 'vestigial', %s)",
            (tenant, shop_domain),
        )

    phone = _uniq_phone()
    order = {
        "customer": {"phone": phone, "first_name": "Resolve", "last_name": "Me"},
        "total_price": "250.00",
        "currency": "INR",
        "created_at": "2026-06-12T10:00:00Z",
    }
    body = json.dumps(order).encode()
    sig = base64.b64encode(
        hmac.new(app_secret.encode(), body, hashlib.sha256).digest()
    ).decode()

    out = asyncio.run(
        shopify_webhook(
            _make_req(body, sig),  # type: ignore[arg-type]
            x_shopify_shop_domain=shop_domain,
            x_shopify_topic="orders/create",
            x_shopify_hmac_sha256=sig,
        )
    )
    assert out["status"] == "ok"
    assert out["rows_committed"] == 1

    # The customer landed under the SHOP-RESOLVED tenant (proves the resolution path).
    with tenant_connection(tenant) as conn:
        cust = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (tenant, phone),
        ).fetchone()
    assert cust is not None, "GAP-2: shop-domain→tenant resolution did not land the row"


@_DB
def test_vt422_webhook_unknown_shop_rejected(db_ctx, monkeypatch):
    """GAP-2: a valid-HMAC delivery for a shop with NO installed tenant is rejected
    (404) — the handler never invents a tenant. Real PG (the zero-row resolution)."""
    import asyncio
    import base64
    import hashlib
    import hmac

    from fastapi import HTTPException

    from orchestrator.api.shopify_webhook import shopify_webhook

    app_secret = "shpss_unknown_shop_test"  # gitleaks:allow — fake test app secret
    monkeypatch.setenv("SHOPIFY_API_SECRET", app_secret)
    body = b'{"customer":{"phone":"+919812345678"},"total_price":"1.00","currency":"INR","created_at":"2026-06-12T10:00:00Z"}'
    sig = base64.b64encode(
        hmac.new(app_secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            shopify_webhook(
                _make_req(body, sig),  # type: ignore[arg-type]
                x_shopify_shop_domain="never-installed.myshopify.com",
                x_shopify_topic="orders/create",
                x_shopify_hmac_sha256=sig,
            )
        )
    assert ei.value.status_code == 404
