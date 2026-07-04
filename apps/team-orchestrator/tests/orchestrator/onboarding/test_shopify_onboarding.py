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
def test_completion_does_not_burst_integration_seam(substrate):
    """VT-576 / CL-2026-07-03: profile-confirm completion NO LONGER bursts the Shopify seam. It sets the
    paced-flow sentinel (``__flow__ = profile_previewed``) and leaves the profile card as the ONLY
    immediate message — the integration onboarding starts only after a later readiness ask + owner yes.
    So there is NO tenant_integration_state right after completion (the old immediate-seam behaviour)."""
    import json

    from orchestrator.onboarding.journey import get_journey, maybe_handle_journey_reply
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

    g = get_journey(tenant)
    assert g is not None and g["status"] == "complete"
    assert g["answers"].get("__flow__") == "profile_previewed", "the paced-flow sentinel is set"
    # The burst is dead: no integration onboarding state exists at completion.
    assert read_integration_state(tenant) is None, "the integration seam must NOT fire on completion"


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


def test_discovery_domain_in_sentence_mints(substrate):
    """VT-588: the owner replies to the address ask with a SENTENCE, not the bare host
    ('here it is: vt425.myshopify.com'). The gate scans the whole body for a valid domain — so it
    still mints the link (never a false 'that's not a store address')."""
    from orchestrator.onboarding.shopify_onboarding import (
        begin_shopify_onboarding,
        maybe_resume_shopify_onboarding,
    )

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)
    result = maybe_resume_shopify_onboarding(
        tenant, "sure, here it is: vt425.myshopify.com", "SID-disco-sentence", recipient=None
    )
    assert result is not None and result["routed"] == "shopify_setup_minted"


def test_discovery_question_falls_through_to_brain(substrate):
    """VT-588: a QUESTION mid-discovery ('what do you charge?') has no domain-shaped token → the gate
    FALLS THROUGH (returns None) so the manager brain answers it — NOT the canned 'that's not a store
    address' reprompt. The pending state stays live so the owner's next domain re-engages the gate."""
    from orchestrator.onboarding.shopify_onboarding import (
        begin_shopify_onboarding,
        maybe_resume_shopify_onboarding,
        read_integration_state,
    )

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)
    for msg in ("what do you charge?", "did you get my store address?",
                "ok back to it, lets continue the setup", "actually how does this work",
                "how do I use shopify?", "wait, what is Shopify exactly",  # bare 'shopify' word ≠ domain attempt
                # Hinglish / mobile no-space-after-period chat — a dotted bigram is NOT a domain attempt
                # (VT-588 adversarial-review finding). These MUST fall through, not canned-reprompt.
                "haan.theek hai", "ok.thanks", "yes.done it", "sure.will do", "no i sell on amazon"):
        result = maybe_resume_shopify_onboarding(tenant, msg, f"SID-q-{abs(hash(msg)) % 9999}", recipient=None)
        assert result is None, f"off-script {msg!r} must fall through to the brain, not canned-reprompt"
    # The connect state stays LIVE — the detour did not abandon the hand-off.
    assert read_integration_state(tenant)["phase"] == "phase_1_discovery"


def test_discovery_malformed_domain_attempt_still_reprompts(substrate, _capture_shopify_sends):
    """VT-588: a genuine but MALFORMED domain attempt ('mystore.shopify.com' wrong TLD) IS a store-
    address attempt → keep the specific reprompt (do NOT fall through — the brain has nothing better to
    say than the exact 'yourstore.myshopify.com' correction)."""
    from orchestrator.onboarding.shopify_onboarding import (
        begin_shopify_onboarding,
        maybe_resume_shopify_onboarding,
    )

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)
    result = maybe_resume_shopify_onboarding(
        tenant, "mystore.shopify.com", "SID-malformed-1", recipient="+919000000123"
    )
    assert result is not None and result["routed"] == "shopify_discovery_retry"
    assert _capture_shopify_sends and "myshopify.com" in _capture_shopify_sends[-1]


def test_onboarding_state_block_surfaces_live_connect_to_brain(substrate):
    """VT-588 (P1b-2): with a live Shopify discovery hand-off, dispatch_brain gets an onboarding-state
    block that names the awaited step (store address) so it can field an off-script message + guide the
    owner back. No integration in flight → no block (the brain isn't told it's onboarding)."""
    from orchestrator.agent.dispatch import _build_onboarding_state_block
    from orchestrator.onboarding.shopify_onboarding import begin_shopify_onboarding

    fresh = _new_tenant(substrate.dsn)
    assert _build_onboarding_state_block(fresh) is None, "no live connect → no onboarding block"

    tenant = _new_tenant(substrate.dsn)
    begin_shopify_onboarding(tenant, recipient=None)  # phase_1_discovery, awaiting store address
    block = _build_onboarding_state_block(tenant)
    assert block is not None
    assert "store address" in block.lower()
    assert "answer their actual message first" in block.lower()  # off-script guidance present
    assert "never claim" in block.lower()  # no-fabrication rail present


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


# =============================================================================
# VT-583 (CL-2026-07-03) — auth-phase converse: floor short-circuit, intent-mediated middle,
# guaranteed reply (the :405-416 silent edge closed). Sends are captured via _send; the
# auth-intent classifier is injected (no live LLM).
# =============================================================================


@pytest.fixture()
def _capture_shopify_sends(monkeypatch):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding import shopify_onboarding

    sent: list[str] = []
    monkeypatch.setattr(shopify_onboarding, "_send", lambda recipient, text, **_kw: sent.append(text or ""))
    return sent


def _mock_auth_intent(monkeypatch, intent):  # type: ignore[no-untyped-def]
    from orchestrator.onboarding import shopify_onboarding

    monkeypatch.setattr(shopify_onboarding, "_llm_classify_auth_intent", lambda _b: intent)


@_DB
def test_auth_floor_done_short_circuits_to_db_recheck(substrate, monkeypatch, _capture_shopify_sends):
    """The unambiguous _DONE floor still wins with ZERO classifier call — it goes straight to the
    authoritative DB re-check, which (no token) refuses to fabricate progress."""
    from orchestrator.onboarding import shopify_onboarding
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    def _boom(_b):
        raise AssertionError("the classifier must not run on an unambiguous _DONE floor hit")

    monkeypatch.setattr(shopify_onboarding, "_llm_classify_auth_intent", _boom)
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")  # phase_2_auth, no token yet
    r = maybe_resume_shopify_onboarding(
        tenant, "done", "SID-auth-floor-1", recipient="+919000000001", connector_factory=_mock_factory
    )
    assert r is not None and r["routed"] == "shopify_auth_not_connected"


@_DB
def test_auth_done_intent_non_floor_reaches_db_recheck(substrate, monkeypatch, _capture_shopify_sends):
    """A NON-floor done-intent ('all set up on my store') is classified 'done' → the SAME authoritative
    DB re-check (still refuses to fabricate when no token exists)."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    _mock_auth_intent(monkeypatch, "done")
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    r = maybe_resume_shopify_onboarding(
        tenant, "all set up on my store", "SID-auth-done-1", recipient="+919000000002",
        connector_factory=_mock_factory,
    )
    assert r is not None and r["routed"] == "shopify_auth_not_connected"


@_DB
def test_auth_done_intent_connected_ingests(substrate, monkeypatch, _capture_shopify_sends):
    """A non-floor done-intent WITH a persisted token → the DB re-check passes → pull+ingest+confirm."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    _mock_auth_intent(monkeypatch, "done")
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    _seed_token(substrate.dsn, tenant)
    r = maybe_resume_shopify_onboarding(
        tenant, "yep finished it on the shopify page", "SID-auth-done-2", recipient="+919000000003",
        connector_factory=_mock_factory,
    )
    assert r is not None and r["routed"] == "shopify_ingested" and r["done"] is True


@_DB
def test_auth_link_intent_remints_fresh_link(substrate, monkeypatch, _capture_shopify_sends):
    """A non-floor 'link' intent re-mints a FRESH authorize link from the stored shop + sends it."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    _mock_auth_intent(monkeypatch, "link")
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")  # writes metadata.shop
    r = maybe_resume_shopify_onboarding(
        tenant, "i cant find the link you sent", "SID-auth-link-1", recipient="+919000000004",
        connector_factory=_mock_factory,
    )
    assert r is not None and r["routed"] == "shopify_auth_link_reminted"
    assert _capture_shopify_sends and "myshopify.com" in _capture_shopify_sends[-1]


@_DB
def test_auth_other_intent_falls_through_to_brain(substrate, monkeypatch, _capture_shopify_sends):
    """VT-597 (mirrors VT-588's discovery-gate conversion): a non-floor question/other reply is a
    genuine off-script message — the classifier POSITIVELY identified it as neither 'done' nor 'link'.
    The gate FALLS THROUGH (returns None) so the manager brain answers it (it has
    _build_onboarding_state_block's awaiting-auth text), instead of the canned honest-waiting line.
    The pending state stays LIVE (oauth_completion, phase_2_auth) so the owner's next 'done' still
    re-engages this gate — the detour does not abandon the hand-off."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        read_integration_state,
        start_shopify_setup,
    )

    _mock_auth_intent(monkeypatch, "other")
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    for msg in ("how long does this usually take", "did you get my store address?",
                "what do you charge?", "actually can I use a different store"):
        r = maybe_resume_shopify_onboarding(
            tenant, msg, f"SID-auth-other-{abs(hash(msg)) % 9999}", recipient="+919000000005",
            connector_factory=_mock_factory,
        )
        assert r is None, f"off-script {msg!r} must fall through to the brain, not the waiting line"
    assert not _capture_shopify_sends, "a fall-through must not also send the gate's own waiting line"
    state = read_integration_state(tenant)
    assert state["phase"] == "phase_2_auth"
    assert state["pending_owner_input"]["awaiting"] == "oauth_completion"


@_DB
def test_auth_silent_edge_absent_walkthrough_still_sends(substrate, monkeypatch, _capture_shopify_sends):
    """VT-583 D3 — the :405-416 silent edge: a non-done reply with NO walkthrough_url on the pending used
    to send NOTHING. Now it ALWAYS sends an honest line (offering a fresh link).

    VT-597 moved the explicit 'other' intent off this path (it now falls through to the brain — see
    ``test_auth_other_intent_falls_through_to_brain``), so this canary now drives the case that still
    owns the honest-waiting-line send: the classifier UNAVAILABLE (None) — no positive signal to
    discriminate, so the deterministic fail-soft reply is what must never go silent."""
    from orchestrator.onboarding import shopify_onboarding
    from orchestrator.onboarding.shopify_onboarding import (
        PHASE_AUTH,
        maybe_resume_shopify_onboarding,
    )

    _mock_auth_intent(monkeypatch, None)
    tenant = _new_tenant(substrate.dsn)
    # A phase_2_auth pending WITHOUT a walkthrough_url (the silent-edge condition).
    pending = shopify_onboarding._validated_pending(
        awaiting="oauth_completion", prompt_text="connect your store", connector_id="shopify",
    )
    assert pending.get("walkthrough_url") is None
    shopify_onboarding._write_state(tenant, phase=PHASE_AUTH, connector_id="shopify", pending=pending)

    r = maybe_resume_shopify_onboarding(
        tenant, "hmm not sure whats going on", "SID-auth-edge-1", recipient="+919000000006",
        connector_factory=_mock_factory,
    )
    assert r is not None and r["routed"] == "shopify_auth_waiting"
    assert _capture_shopify_sends, "the absent-walkthrough path MUST still send something (no silent drop)"
    assert "link" in _capture_shopify_sends[-1].lower()


@_DB
def test_auth_classifier_unavailable_still_sends(substrate, monkeypatch, _capture_shopify_sends):
    """Classifier unavailable (returns None, e.g. no live key) → NO positive 'other' signal, so the
    gate does NOT fall through blind — it keeps the deterministic honest-waiting line (VT-597: falling
    through is reserved for a POSITIVE 'other' classification only). Fail-soft = a guaranteed reply,
    never silence, never an unclassified message left to the brain unsupervised."""
    from orchestrator.onboarding.shopify_onboarding import (
        maybe_resume_shopify_onboarding,
        start_shopify_setup,
    )

    _mock_auth_intent(monkeypatch, None)  # classifier degrades to None (no key / error)
    tenant = _new_tenant(substrate.dsn)
    start_shopify_setup(tenant, "vt425.myshopify.com")
    r = maybe_resume_shopify_onboarding(
        tenant, "achha ji dekhta hoon", "SID-auth-nokey-1", recipient="+919000000007",
        connector_factory=_mock_factory,
    )
    assert r is not None and r["routed"] == "shopify_auth_waiting"
    assert _capture_shopify_sends, "fail-soft must still send an honest line (no silence)"


def test_resume_gate_send_threads_tenant_id_to_conversation_log(monkeypatch):
    """VT-586: shopify_onboarding._send (every resume-gate reply — shop-domain retry, auth waiting line,
    not-connected re-prompt, connected confirm) must pass tenant_id + surface='journey' into
    send_freeform_message, so the integration hand-off is recorded to the lifetime conversation_log.
    Before VT-586 these reached the owner's phone but the Team-Manager's 24h window lost the entire
    hand-off, and the server harness read every resume reply as false 'silence'."""
    from orchestrator.onboarding import shopify_onboarding
    from orchestrator.utils import twilio_send

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        twilio_send, "send_freeform_message",
        lambda body, recipient, **kw: captured.update({"body": body, **kw}) or "SM0",
    )
    tid = uuid4()
    shopify_onboarding._send("+919000000099", "please finish approving, then reply 'done'", tenant_id=tid)
    assert captured.get("tenant_id") == tid, "resume-gate _send must thread tenant_id into the record choke"
    assert captured.get("surface") == "journey"
