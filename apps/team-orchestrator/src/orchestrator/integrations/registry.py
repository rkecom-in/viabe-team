"""VT-205 — Connector registry (16 initial entries).

The registry is the Integration Agent's structured knowledge base. One
entry per supported tool. Concrete implementations live in
``connectors/<id>.py`` (VT-207 ships google_sheet; VT-208 ships
shopify). Entries marked ``implementation_vt_row=""`` are stubs that
point to existing VT-6 family rows (paper_book / contacts / etc.).

Per CL-420: VT-205 is the spec layer. Real SDK wiring is per-connector
follow-up rows.
Per CL-19: every entry validates against ``ConnectorSpec`` at boot
(test surface) and on every PR via the schema-drift CI gate.
"""

from __future__ import annotations

from orchestrator.integrations.schemas import (
    CategoryKind,
    ConnectorSpec,
    RateLimitSpec,
    SamplePullSpec,
)


def _stub_sample_pull(method: str = "owner_input") -> SamplePullSpec:
    return SamplePullSpec(method=method, config_hints={}, expected_row_count=0)  # type: ignore[arg-type]


REGISTRY: dict[str, ConnectorSpec] = {
    # ---------------- Implementable now (2) ----------------
    "google_sheet": ConnectorSpec(
        connector_id="google_sheet",
        display_name="Google Sheets",
        category="digital",
        auth_flow="oauth2",
        auth_scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        auth_walkthrough_url="https://support.google.com/cloud/answer/6158849",
        sample_pull=SamplePullSpec(
            method="sheet_range",
            config_hints={"range_default": "Sheet1!A1:Z50"},
            expected_row_count=50,
        ),
        canonical_fields_hints={
            "customer_name": ["Customer", "Name", "Full Name"],
            "phone": ["Phone", "Mobile", "Customer Phone"],
            "email": ["Email", "Email Address"],
            "order_amount": ["Amount", "Total", "Order Value"],
            "order_date": ["Date", "Order Date", "Created At"],
        },
        rate_limits=RateLimitSpec(
            requests_per_minute=60,
            requests_per_day=100_000,
            notes="Sheets API quota per project; per-user 60 req/min default.",
        ),
        push_supported=True,
        pull_default_cadence="0 9 * * *",
        implementation_vt_row="VT-207",
        summary="Pull customer/order rows from a Google Sheets tab via read-only OAuth.",
    ),
    "shopify": ConnectorSpec(
        connector_id="shopify",
        display_name="Shopify",
        category="digital",
        auth_flow="api_key",
        auth_scopes=["read_orders", "read_customers", "read_products"],
        auth_walkthrough_url="https://help.shopify.com/en/manual/apps/app-types/custom-apps",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={
                "endpoint": "/admin/api/2024-07/orders.json",
                "page_size": "50",
            },
            expected_row_count=50,
        ),
        canonical_fields_hints={
            "customer_name": ["customer.first_name", "customer.last_name"],
            "phone": ["customer.phone", "shipping_address.phone"],
            "email": ["customer.email"],
            "order_amount": ["total_price"],
            "order_date": ["created_at"],
        },
        rate_limits=RateLimitSpec(
            requests_per_minute=240,
            requests_per_day=100_000,
            notes="Shopify REST API: 4 req/sec leaky bucket per shop.",
        ),
        push_supported=True,
        pull_default_cadence="0 */6 * * *",
        implementation_vt_row="VT-208",
        summary="Pull orders + customers from Shopify admin via private app token.",
    ),
    # ---------------- Digital placeholders (6) ----------------
    "google_analytics_4": ConnectorSpec(
        connector_id="google_analytics_4",
        display_name="Google Analytics 4",
        category="digital",
        auth_flow="oauth2",
        auth_scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        auth_walkthrough_url="https://support.google.com/analytics/answer/10089681",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/v1beta/properties/{id}:runReport"},
            expected_row_count=50,
        ),
        canonical_fields_hints={"session_id": ["sessionId"], "event_name": ["eventName"]},
        rate_limits=RateLimitSpec(requests_per_minute=10, requests_per_day=10_000),
        push_supported=False,
        pull_default_cadence="0 9 * * *",
        implementation_vt_row="",
        summary="Pull GA4 event data for traffic + conversion attribution.",
    ),
    "gohighlevel_crm": ConnectorSpec(
        connector_id="gohighlevel_crm",
        display_name="GoHighLevel CRM",
        category="digital",
        auth_flow="api_key",
        auth_scopes=["contacts.read", "opportunities.read"],
        auth_walkthrough_url="https://help.gohighlevel.com/support/solutions/articles/48000",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/contacts"},
            expected_row_count=50,
        ),
        canonical_fields_hints={"phone": ["phone"], "email": ["email"]},
        rate_limits=RateLimitSpec(requests_per_minute=100, requests_per_day=10_000),
        push_supported=True,
        pull_default_cadence="0 */6 * * *",
        implementation_vt_row="",
        summary="Pull contacts + opportunities from GoHighLevel CRM.",
    ),
    "woocommerce": ConnectorSpec(
        connector_id="woocommerce",
        display_name="WooCommerce",
        category="digital",
        auth_flow="api_key",
        auth_scopes=["read"],
        auth_walkthrough_url="https://woocommerce.github.io/woocommerce-rest-api-docs/",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/wp-json/wc/v3/orders"},
            expected_row_count=50,
        ),
        canonical_fields_hints={
            "customer_name": ["billing.first_name", "billing.last_name"],
            "phone": ["billing.phone"],
            "email": ["billing.email"],
        },
        rate_limits=RateLimitSpec(requests_per_minute=60, requests_per_day=10_000),
        push_supported=True,
        pull_default_cadence="0 */6 * * *",
        implementation_vt_row="",
        summary="Pull orders + customers from a WooCommerce store via REST API.",
    ),
    "razorpay": ConnectorSpec(
        connector_id="razorpay",
        display_name="Razorpay",
        category="digital",
        auth_flow="api_key",
        auth_scopes=["read_only"],
        auth_walkthrough_url="https://razorpay.com/docs/api/authentication/",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/v1/payments"},
            expected_row_count=50,
        ),
        canonical_fields_hints={
            "phone": ["contact"],
            "email": ["email"],
            "order_amount": ["amount"],
            "order_date": ["created_at"],
        },
        rate_limits=RateLimitSpec(requests_per_minute=120, requests_per_day=100_000),
        push_supported=True,
        pull_default_cadence="0 */6 * * *",
        implementation_vt_row="",
        summary="Pull payment + customer data from Razorpay.",
    ),
    "amazon_seller_central": ConnectorSpec(
        connector_id="amazon_seller_central",
        display_name="Amazon Seller Central (SP-API)",
        category="digital",
        auth_flow="oauth2",
        auth_scopes=["sellingpartnerapi::notifications"],
        auth_walkthrough_url="https://developer-docs.amazon.com/sp-api/docs/registering-as-a-developer",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/orders/v0/orders"},
            expected_row_count=50,
        ),
        canonical_fields_hints={
            "customer_name": ["BuyerName"],
            "order_amount": ["OrderTotal.Amount"],
            "order_date": ["PurchaseDate"],
        },
        rate_limits=RateLimitSpec(requests_per_minute=10, requests_per_day=10_000),
        push_supported=True,
        pull_default_cadence="0 */6 * * *",
        implementation_vt_row="",
        summary="Pull Amazon orders via SP-API.",
    ),
    "meta_ads_pixel": ConnectorSpec(
        connector_id="meta_ads_pixel",
        display_name="Meta Ads + Pixel",
        category="digital",
        auth_flow="oauth2",
        auth_scopes=["ads_read"],
        auth_walkthrough_url="https://developers.facebook.com/docs/marketing-apis/overview",
        sample_pull=SamplePullSpec(
            method="rest_paginated",
            config_hints={"endpoint": "/act_{ad_account_id}/insights"},
            expected_row_count=50,
        ),
        canonical_fields_hints={},
        rate_limits=RateLimitSpec(requests_per_minute=200, requests_per_day=100_000),
        push_supported=True,
        pull_default_cadence="0 9 * * *",
        implementation_vt_row="",
        summary="Pull Meta ad spend + pixel conversion data.",
    ),
    # ---------------- Manual VT-6 family stubs (8) ----------------
    "paper_book": ConnectorSpec(
        connector_id="paper_book",
        display_name="Paper book / register",
        category="manual",
        auth_flow="manual_upload",
        sample_pull=_stub_sample_pull("csv_upload"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-52",
        summary="Owner photos pages of a paper register; OCR via Apify-style worker.",
    ),
    "contacts": ConnectorSpec(
        connector_id="contacts",
        display_name="Phone contacts export (vCard / CSV)",
        category="manual",
        auth_flow="manual_upload",
        sample_pull=_stub_sample_pull("csv_upload"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-53",
        summary="Owner uploads vCard / CSV from phone contacts.",
    ),
    "upi_export": ConnectorSpec(
        connector_id="upi_export",
        display_name="UPI app statement export",
        category="manual",
        auth_flow="manual_upload",
        sample_pull=_stub_sample_pull("csv_upload"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-54",
        summary="Owner exports transactions from PhonePe/GPay/Paytm.",
    ),
    "kot_pos": ConnectorSpec(
        connector_id="kot_pos",
        display_name="KOT / POS terminal export",
        category="manual",
        auth_flow="manual_upload",
        sample_pull=_stub_sample_pull("csv_upload"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-55",
        summary="Owner exports sales data from KOT/POS terminal (restaurant/retail).",
    ),
    "cash_book": ConnectorSpec(
        connector_id="cash_book",
        display_name="Cash book / khata",
        category="manual",
        auth_flow="manual_upload",
        sample_pull=_stub_sample_pull("csv_upload"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-56",
        summary="Owner sends khata / cash-book pages for OCR.",
    ),
    "qr_opt_in": ConnectorSpec(
        connector_id="qr_opt_in",
        display_name="QR-code customer opt-in",
        category="manual",
        auth_flow="none",
        sample_pull=_stub_sample_pull("owner_input"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-57",
        summary="Customer scans QR at till + opts-in to WhatsApp.",
    ),
    "apify_scrape": ConnectorSpec(
        connector_id="apify_scrape",
        display_name="Public-data scrape (Apify)",
        category="scrape",
        auth_flow="api_key",
        sample_pull=_stub_sample_pull("rest_paginated"),
        rate_limits=RateLimitSpec(requests_per_minute=30, requests_per_day=5_000),
        implementation_vt_row="VT-58",
        summary="Use Apify actors to scrape public sources (Justdial / Google Maps).",
    ),
    "owner_typed": ConnectorSpec(
        connector_id="owner_typed",
        display_name="Owner-typed (in WhatsApp)",
        category="manual",
        auth_flow="none",
        sample_pull=_stub_sample_pull("owner_input"),
        rate_limits=RateLimitSpec(requests_per_minute=0, requests_per_day=0),
        implementation_vt_row="VT-59",
        summary="Owner types customer entries directly into WhatsApp; the agent structures them.",
    ),
}


def get_connector(connector_id: str) -> ConnectorSpec:
    """Look up a registry entry. Raises ``KeyError`` on unknown id."""
    if connector_id not in REGISTRY:
        raise KeyError(
            f"connector '{connector_id}' not in registry; available: "
            f"{sorted(REGISTRY.keys())}"
        )
    return REGISTRY[connector_id]


def list_connectors(
    category: CategoryKind | None = None,
) -> list[ConnectorSpec]:
    """List registry entries, optionally filtered by category.

    Deterministic order: sorted by connector_id.
    """
    items = sorted(REGISTRY.values(), key=lambda s: s.connector_id)
    if category is None:
        return items
    return [s for s in items if s.category == category]


__all__ = ["REGISTRY", "get_connector", "list_connectors"]
