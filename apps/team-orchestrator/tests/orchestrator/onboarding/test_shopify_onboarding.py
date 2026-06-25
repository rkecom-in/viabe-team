"""VT-425 Phase A — conversational Shopify onboarding tests + PII/resume canaries.

PURE: the fixed-schema Shopify SAMPLE-row → CanonicalRow auto-map (customers + abandoned
checkouts; identity anchors; INR-only sale; address/line-item drop; no-anchor → None).

DB (real Postgres, no mock cursors — the VT-263 / Cowork bar): the conversational flow
end-to-end with an INJECTED mock Shopify connector (the live OAuth+pull is deferred to Fazal's
VT-422 Partner app):
  * journey completes → SEAM (begin_shopify_onboarding) writes phase_1_discovery + connector_choice.
  * discovery inbound (shop domain) → start_shopify_setup mints a real authorize_url + phase_2_auth.
  * RESUME canary: an inbound "done" after the link-out RE-CHECKS connector status (DB truth) and
    continues — it does NOT re-enter the brain fresh.
  * not-connected guard: "done" with no token row → stays phase_2_auth, NO fabricated progress.
  * connected → pull_orders (MOCK) → fixed-schema auto-map → ingest_customer_rows writes real
    customers + ledger rows; phase_5_confirmed.
  * PII canary: counts-only owner prompt + counts-only logging; NO raw phone/email persisted from a
    fabricated number (the mock fixture is clearly-marked; never a DB-persisted fake number that
    wasn't in the explicit fixture).

Real external-customer pull is post-VT-231 (CL-422); this proves the WIRING with a mock connector.
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.connectors.shopify import (  # noqa: E402
    shopify_sample_row_to_canonical,
)


# =============================================================================
# PURE — fixed-schema sample-row auto-map (no DB, no LLM)
# =============================================================================


def test_sample_customer_identity_only():
    row = shopify_sample_row_to_canonical(
        {
            "__source": "customers",
            "phone": "+919876500001",
            "email": "Asha@Example.COM",
            "first_name": "Asha",
            "last_name": "K",
            "default_address": {"address1": "DROP ME"},
        }
    )
    assert row is not None
    assert row.phone_e164 == "+919876500001"
    assert row.email == "asha@example.com"  # lowercased
    assert row.display_name == "Asha K"
    assert row.sales == ()  # a customer record carries no sale
    assert row.consent is None  # option A


def test_sample_customer_default_address_phone_fallback():
    row = shopify_sample_row_to_canonical(
        {"__source": "customers", "first_name": "Bina",
         "default_address": {"phone": "9876500002"}}
    )
    assert row is not None
    assert row.phone_e164 == "+919876500002"  # bare 10-digit IN normalized


def test_sample_abandoned_checkout_has_sale():
    row = shopify_sample_row_to_canonical(
        {
            "__source": "abandoned_checkouts",
            "customer": {"phone": "+919876500003", "first_name": "Chetan"},
            "shipping_address": {"address1": "DROP ME", "phone": "+919876500003"},
            "line_items": [{"title": "DROP ME", "price": "499.00"}],
            "total_price": "499.00",
            "currency": "INR",
            "created_at": "2026-06-01T00:00:00Z",
        }
    )
    assert row is not None
    assert row.phone_e164 == "+919876500003"
    assert row.display_name == "Chetan"
    assert len(row.sales) == 1
    assert row.sales[0].amount_paise == 49900
    assert row.sales[0].entry_date == date(2026, 6, 1)


def test_sample_abandoned_checkout_non_inr_keeps_identity_skips_sale():
    row = shopify_sample_row_to_canonical(
        {
            "__source": "abandoned_checkouts",
            "customer": {"email": "intl@example.com"},
            "total_price": "20.00",
            "currency": "USD",
            "created_at": "2026-06-01T00:00:00Z",
        }
    )
    assert row is not None
    assert row.email == "intl@example.com"
    assert row.sales == ()  # no FX → no sale (currency guard)


def test_sample_drops_address_and_lineitems():
    # PII boundary (§3): only identity + sale magnitude survive into CanonicalRow.
    row = shopify_sample_row_to_canonical(
        {
            "__source": "abandoned_checkouts",
            "customer": {"phone": "+919000000009", "first_name": "Dev"},
            "shipping_address": {"address1": "12 MG Road"},
            "line_items": [{"title": "Widget"}],
            "total_price": "100.00",
            "currency": "INR",
            "created_at": "2026-06-01T00:00:00Z",
        }
    )
    assert row is not None
    assert set(row.model_dump().keys()) == {
        "phone_e164", "email", "display_name", "sales", "consent"
    }


def test_sample_no_anchor_returns_none():
    assert shopify_sample_row_to_canonical(
        {"__source": "customers", "first_name": "", "last_name": ""}
    ) is None
    assert shopify_sample_row_to_canonical(
        {"__source": "abandoned_checkouts", "total_price": "100.00", "currency": "INR"}
    ) is None


# =============================================================================
# DB + CANARIES (real Postgres) — the conversational flow with a MOCK connector
# =============================================================================

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-425 Shopify onboarding DB/canary tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    if not os.environ.get("TEAM_PHONE_HASH_SALT"):
        os.environ["TEAM_PHONE_HASH_SALT"] = "vt425-canary-salt"
    # Shopify OAuth env so build_oauth_install_url can mint a real authorize_url (no network call —
    # build_oauth_install_url is a pure URL builder; only the token exchange hits the network).
    os.environ.setdefault("SHOPIFY_API_KEY", "cid_test_vt425")
    os.environ.setdefault("SHOPIFY_API_SECRET", "secret_test_vt425")  # gitleaks:allow — fake test value
    os.environ.setdefault(
        "SHOPIFY_OAUTH_REDIRECT_URI",
        "https://viabe-team-dev.vercel.app/api/orchestrator/integrations/shopify/oauth/callback",
    )

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, business_type, whatsapp_number) "
            "VALUES ('VT-425 shopify onboarding test', 'founding', 'onboarding', 'restaurant', %s) "
            "RETURNING id",
            (f"+9199{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_token(dsn: str, tenant_id: UUID) -> None:
    """Simulate the OAuth callback having persisted a token — the DB-truth resume signal."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_oauth_tokens "
            "(tenant_id, connector_id, refresh_token_encrypted, scopes, push_secret, shop_url) "
            "VALUES (%s, 'shopify', 'enc', ARRAY['read_customers'], 'psh', 'vt425.myshopify.com') "
            "ON CONFLICT (tenant_id, connector_id) DO NOTHING",
            (str(tenant_id),),
        )


# A clearly-marked MOCK Shopify connector — NEVER a fabricated DB-persisted number outside the
# explicit fixture rows. These are obviously-synthetic test phones/emails (CL-422: dev = internal
# data only; the live pull against real customer data is post-VT-231 / Fazal's VT-422 Partner app).
class _MockShopifyConnector:
    def pull_orders(self, tenant_id: UUID):  # noqa: ARG002 — signature parity with the real connector
        # VT-447: onboarding ingests real ORDERS (/orders.json), NOT abandoned checkouts. Each order
        # carries a sale. ``processed_at`` is the real transaction date the mapper reads (the field that
        # also sticks on a backdated/API order — Shopify forces ``created_at``=now on creation).
        return [
            {"customer": {"phone": "+919800000001", "first_name": "MockA",
                          "last_name": "Test", "email": "mocka@example.test"},
             "total_price": "500.00", "currency": "INR", "processed_at": "2026-06-09T08:15:00Z"},
            {"customer": {"phone": "+919800000002", "first_name": "MockB"},
             "total_price": "750.00", "currency": "INR", "processed_at": "2026-06-10T08:15:00Z"},
        ]


def _mock_factory():
    return _MockShopifyConnector()


@_DB
def test_seam_journey_complete_begins_shopify_onboarding(substrate):
    """SEAM: journey completion hands off → phase_1_discovery + connector_choice pending."""
    from orchestrator.onboarding.shopify_onboarding import (
        begin_shopify_onboarding,
        read_integration_state,
    )

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)  # recipient None → no send, state still written
    state = read_integration_state(tenant)
    assert state is not None
    assert state["phase"] == "phase_1_discovery"
    assert state["current_connector_id"] == "shopify"
    assert state["pending_owner_input"]["awaiting"] == "connector_choice"


@_DB
def test_seam_fires_on_real_journey_completion(substrate):
    """SEAM end-to-end: a real journey reply that COMPLETES the journey (via
    maybe_handle_journey_reply) triggers begin_shopify_onboarding — the owner is handed straight
    from profile-confirm into connector onboarding, never dropped into a cold brain."""
    import json

    from orchestrator.onboarding.journey import maybe_handle_journey_reply
    from orchestrator.onboarding.shopify_onboarding import read_integration_state

    tenant = _new_tenant(substrate.dsn)
    # Seed an active journey at its LAST question so a single confirm reply completes it.
    q = [{"field": "business_name", "kind": "confirm", "prompt_en": "Is your name X?",
          "prompt_hi": "", "draft_value": "X"}]
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO onboarding_journey "
            "(tenant_id, status, question_queue, cursor, answers, skipped) "
            "VALUES (%s, 'active', %s, 0, '{}'::jsonb, '[]'::jsonb)",
            (str(tenant), json.dumps(q)),
        )

    r = maybe_handle_journey_reply(tenant, "yes", "SID-seam-real-1", None)
    assert r is not None and r.get("done") is True  # journey completed on this reply

    state = read_integration_state(tenant)
    assert state is not None, "SEAM did not fire: no integration state after journey completion"
    assert state["phase"] == "phase_1_discovery"
    assert state["current_connector_id"] == "shopify"


@_DB
def test_discovery_inbound_mints_authorize_url(substrate):
    """RESUME (discovery): the owner's shop-domain reply mints a REAL authorize_url + phase_2_auth."""
    from orchestrator.onboarding.shopify_onboarding import (
        begin_shopify_onboarding,
        maybe_resume_shopify_onboarding,
        read_integration_state,
    )

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)
    result = maybe_resume_shopify_onboarding(
        tenant, "vt425.myshopify.com", "SID-disco-1", recipient=None
    )
    assert result is not None
    assert result["routed"] == "shopify_setup_minted"
    state = read_integration_state(tenant)
    assert state["phase"] == "phase_2_auth"
    pending = state["pending_owner_input"]
    assert pending["awaiting"] == "oauth_completion"
    # The authorize_url is real (built off build_oauth_install_url): Shopify host + the state nonce.
    url = pending["walkthrough_url"]
    assert url.startswith("https://vt425.myshopify.com/admin/oauth/authorize?")
    assert "state=" in url and "client_id=" in url


@_DB
def test_resume_canary_not_connected_does_not_fabricate(substrate):
    """RESUME guard: 'done' with NO token row → stays phase_2_auth, NO ingest, NO fabrication."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        read_integration_state,
        start_shopify_setup,
    )

    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")  # phase_2_auth, oauth_completion pending
    result = maybe_resume_shopify_onboarding(
        tenant, "done", "SID-notconn-1", recipient=None, connector_factory=_mock_factory
    )
    assert result is not None
    assert result["routed"] == "shopify_auth_not_connected"
    assert read_integration_state(tenant)["phase"] == "phase_2_auth"  # did NOT advance
    # No customers were written (no fabricated rows).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) FROM customers WHERE tenant_id = %s", (str(tenant),)
        ).fetchone()[0]
    assert n == 0


@_DB
def test_resume_canary_connected_pulls_maps_ingests(substrate):
    """THE PROOF + RESUME canary: connected → 'done' inbound RESUMES (re-checks status), pulls the
    MOCK orders, fixed-schema auto-maps, ingests real customers+ledger, advances phase_5_confirmed."""
    from orchestrator.db import tenant_connection
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        read_integration_state,
        start_shopify_setup,
    )

    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    _seed_token(substrate.dsn, tenant)  # the OAuth callback persisted a token → connected

    result = maybe_resume_shopify_onboarding(
        tenant, "done", "SID-conn-1", recipient=None, connector_factory=_mock_factory
    )
    assert result is not None
    assert result["routed"] == "shopify_ingested"
    assert result["done"] is True
    assert result["committed"] == 2  # both mock rows had an anchor
    assert read_integration_state(tenant)["phase"] == "phase_5_confirmed"

    # Real customers + a real `sale` ledger row (from the abandoned-checkout) landed.
    with tenant_connection(tenant) as conn:
        custs = conn.execute(
            "SELECT phone_e164, acquired_via FROM customers WHERE tenant_id = %s ORDER BY phone_e164",
            (str(tenant),),
        ).fetchall()
        phones = {c["phone_e164"] for c in custs}
        assert phones == {"+919800000001", "+919800000002"}
        assert all("shopify" in (c["acquired_via"] or []) for c in custs)
        sales = conn.execute(
            "SELECT amount_paise, entry_type FROM customer_ledger_entries WHERE tenant_id = %s",
            (str(tenant),),
        ).fetchall()
    # VT-447: each real ORDER carries a sale (both mock orders are INR).
    assert len(sales) == 2
    assert {s["amount_paise"] for s in sales} == {50000, 75000}
    assert all(s["entry_type"] == "sale" for s in sales)

    # A recurring-ingestion schedule was set.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        sched = conn.execute(
            "SELECT enabled FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = 'shopify'",
            (str(tenant),),
        ).fetchone()
    assert sched is not None and sched[0] is True


@_DB
def test_pii_canary_counts_only_no_raw_pii_in_prompt(substrate, caplog):
    """PII canary: the owner-facing confirm prompt is COUNTS ONLY (no raw phone/email/name), and
    logging is counts-only — no raw customer PII reaches the prompt text or the log stream."""
    import logging

    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        read_integration_state,
        start_shopify_setup,
    )

    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    _seed_token(substrate.dsn, tenant)

    with caplog.at_level(logging.INFO, logger="orchestrator.onboarding.shopify_onboarding"):
        maybe_resume_shopify_onboarding(
            tenant, "done", "SID-pii-1", recipient=None, connector_factory=_mock_factory
        )

    prompt = read_integration_state(tenant)["pending_owner_input"]["prompt_text"]
    raw_pii = ["+919800000001", "+919800000002", "mocka@example.test", "MockA", "MockB"]
    for token in raw_pii:
        assert token not in prompt, f"raw PII leaked into owner prompt: {token}"
        assert token not in caplog.text, f"raw PII leaked into logs: {token}"
    # The prompt carries the COUNT instead.
    assert "2 customers" in prompt


@_DB
def test_resume_canary_does_not_reenter_brain_fresh(substrate, monkeypatch):
    """RESUME canary (the gap closed): an inbound after the link-out is consumed by the resume hook
    (returns a result), so the runner short-circuits — it does NOT fall through to the brain."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    _seed_token(substrate.dsn, tenant)

    # The hook RETURNS a non-None result → the runner gate treats the message as consumed and never
    # reaches dispatch_brain. (A None return would mean "fall through to the brain fresh".)
    result = maybe_resume_shopify_onboarding(
        tenant, "done", "SID-noreenter-1", recipient=None, connector_factory=_mock_factory
    )
    assert result is not None  # consumed → brain NOT re-entered


@_DB
def test_opt_out_falls_through_not_consumed(substrate):
    """DPDP: an opt-out during onboarding is NOT consumed by the resume hook (falls through to
    pre_filter's authoritative opt-out handler)."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    _seed_token(substrate.dsn, tenant)
    assert maybe_resume_shopify_onboarding(
        tenant, "STOP", "SID-optout-1", recipient=None, connector_factory=_mock_factory
    ) is None
