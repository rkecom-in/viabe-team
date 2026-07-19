"""VT-604 Package 1 — the owner-visible connector catalogue is Shopify + Google Sheets ONLY.

Placeholder connectors in the VT-205 registry (Amazon Seller Central, GA4, WooCommerce,
the manual VT-6 family, …) must never be presented as something the owner can actually
connect today. Three presentation surfaces are covered:

  1. ``list_supported_connectors`` (integration_agent) — the owner-facing connector listing.
  2. ``render_connector_listing_markdown`` — the Integration Agent's own system-prompt
     catalogue block (so the model never even SEES a placeholder as "available").
  3. ``start_oauth`` — "Connect Amazon" must produce an honest ``unsupported``
     response with NO promised follow-up action (no walkthrough, no "coming soon").
  4. ``advise_integration_setup`` (tech_lane) — the Tech advisory tool's setup-advice
     surface.

VT-608 (Loop Package 5) renamed the underlying tools (list_connectors_tool -> list_supported_
connectors; start_connector_setup -> start_oauth) — this file follows that rename; the
acceptance criteria it proves are unchanged.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

from orchestrator.observability.decorators import observability_context  # noqa: E402


def test_owner_visible_connector_ids_is_shopify_and_google_sheet_only() -> None:
    from orchestrator.integrations import OWNER_VISIBLE_CONNECTOR_IDS

    assert OWNER_VISIBLE_CONNECTOR_IDS == {"shopify", "google_sheet"}


def test_list_connectors_tool_never_lists_a_placeholder() -> None:
    from orchestrator.agent.integration_agent import list_supported_connectors

    out = list_supported_connectors.func()  # type: ignore[attr-defined]
    assert "shopify" in out
    assert "google_sheet" in out
    for placeholder in ("amazon_seller_central", "google_analytics_4", "woocommerce", "razorpay"):
        assert placeholder not in out


def test_system_prompt_connector_block_never_advertises_a_placeholder() -> None:
    from orchestrator.integrations import render_connector_listing_markdown

    block = render_connector_listing_markdown()
    assert "shopify" in block
    assert "google_sheet" in block
    for placeholder in (
        "amazon_seller_central",
        "google_analytics_4",
        "woocommerce",
        "gohighlevel_crm",
        "meta_ads_pixel",
        "paper_book",
        "apify_scrape",
    ):
        assert placeholder not in block


def test_connect_amazon_produces_honest_unsupported_response_no_promised_followup() -> None:
    """The acceptance criterion, verbatim: 'Connect Amazon' produces an honest unsupported
    response with no promised follow-up action."""
    from orchestrator.agent.integration_agent import start_oauth

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = start_oauth.func(  # type: ignore[attr-defined]
            connector_id="amazon_seller_central", tenant_id=str(tenant_id)
        )

    assert out["status"] == "unsupported"
    assert out["connector_id"] == "amazon_seller_central"
    # No promised follow-up: none of the "there's a path forward" keys the wired
    # connectors return (an auth walkthrough, a next_action, an authorize_url).
    assert "next_action" not in out
    assert "authorize_url" not in out
    assert "not_wired_phase_a" not in out


def test_start_connector_setup_still_wires_shopify() -> None:
    """The filter must not regress the ONE real, live connector path."""
    from orchestrator.agent.integration_agent import start_oauth

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = start_oauth.func(  # type: ignore[attr-defined]
            connector_id="shopify", tenant_id=str(tenant_id)
        )

    assert out["connector_id"] == "shopify"
    assert out["next_action"] == "prompt_shop_domain"
    assert "status" not in out or out.get("status") != "unsupported"


def test_advise_integration_setup_never_recommends_a_placeholder() -> None:
    from orchestrator.agent.tech_lane import advise_integration_setup

    out = advise_integration_setup.func(category="digital")  # type: ignore[attr-defined]
    ids = {c["connector_id"] for c in out["connectors"]}
    assert "shopify" in ids
    assert "amazon_seller_central" not in ids
    assert "google_analytics_4" not in ids
