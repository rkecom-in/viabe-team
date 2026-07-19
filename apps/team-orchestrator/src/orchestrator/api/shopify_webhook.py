"""VT-208 / VT-417 Shopify webhook router. REWORKED by VT-422 (GAP-2) to the real
app-delivery auth model.

Endpoint: ``POST /api/orchestrator/integrations/shopify/webhook``.

Receives 4 topics: ``checkouts/create``, ``checkouts/update``,
``orders/create``, ``orders/paid``.

VT-422 GAP-2 — the real public-app delivery model
-------------------------------------------------
The webhook is registered via the Admin API (``POST webhooks.json``, see
``ShopifyConnector.setup_push``). For an APP-registered webhook, Shopify:

  * does NOT inject any custom ``X-Viabe-Tenant`` header (the webhooks.json body
    carries no header-injection field), and
  * signs the delivery with the APP's ``client_secret`` (``SHOPIFY_API_SECRET``),
    base64 over the RAW body — NOT a per-tenant locally-minted ``push_secret``.

So the old model (require ``X-Viabe-Tenant``; verify against ``push_secret``) would
400 on every real delivery (no tenant header) and 403 even if it didn't (wrong
secret). The push_secret model only worked for the retired Apps-Script/sheet push
path that DID inject the header. The reworked handler:

  * Tenant resolution: derive the tenant from the ``X-Shopify-Shop-Domain`` header
    → ``SELECT tenant_id FROM tenant_oauth_tokens WHERE connector_id='shopify' AND
    shop_url = <domain>`` (service pool; index in migration 141). Reject if zero
    (404-equiv) or ambiguous (multiple tenants on the same shop — never trust).
  * HMAC: verify ``X-Shopify-Hmac-Sha256`` (base64 over the raw body) against the
    APP ``client_secret`` (``SHOPIFY_API_SECRET``) via ``verify_push_signature``,
    keyed on the app secret — NOT ``push_secret``. (Webhook-vs-callback HMAC
    encodings already correctly differ: base64 body here vs hex query in the OAuth
    callback.)

``push_secret`` becomes vestigial for the OAuth path (the column stays for the
sheet path); the Shopify webhook verify no longer relies on it.

VT-417 — the inbound lineage writes the REAL customer substrate. Topic routing:

- ``orders/create`` → SALE-OF-RECORD. Map the order → ``CanonicalRow`` →
  ``ingest_customer_rows(acquired_via="shopify")`` → a real ``customers`` row +
  ONE ``sale`` ``customer_ledger_entries`` row (idempotent on re-delivery via the
  ledger ``entry_key``). Consent is NEVER written from Shopify (option A, §2.4).
- ``orders/paid`` → ATTRIBUTION-ONLY. Logs the match intent; NO second ledger write.
- ``checkouts/*`` → DEFERRED (abandoned-checkout substrate, a separate feature).

Per CL-72 the handler returns 2xx whenever possible; 401/403 on a bad
shop/signature is the one allowed non-2xx so Shopify stops retrying a forged call.
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.shopify import (
    ShopifyConnector,
    shopify_order_to_canonical,
)
from orchestrator.integrations.ingest import ingest_customer_rows

logger = logging.getLogger(__name__)
router = APIRouter()

_ACQUIRED_VIA = "shopify"
_CONNECTOR_ID = "shopify"


def _resolve_tenant_from_shop(shop_domain: str) -> UUID:
    """Resolve the tenant from the ``X-Shopify-Shop-Domain`` header.

    VT-422 GAP-2: the ONLY tenant linkage on an app-delivered Shopify webhook is the
    shop domain. Look it up against the per-tenant OAuth-install record. Reject:
      * zero rows → 404-equiv (no installed tenant for this shop), and
      * >1 row    → ambiguous (refuse rather than guess which tenant owns it).

    Runs on the service pool (BYPASSRLS) — the callback has no tenant GUC, the shop
    domain IS the resolution key. Backed by the migration-141 index.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM tenant_oauth_tokens "
            "WHERE connector_id = %s AND shop_url = %s",
            (_CONNECTOR_ID, shop_domain),
        )
        rows = cur.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="no installed tenant for shop")
    if len(rows) > 1:
        # Ambiguous — multiple tenants claim the same shop. Never guess.
        logger.warning(
            "shopify webhook: ambiguous shop resolution (%d tenants) shop=%s",
            len(rows), shop_domain,
        )
        raise HTTPException(status_code=409, detail="ambiguous shop resolution")
    raw = cast("dict[str, Any]", rows[0])
    tenant_value = raw["tenant_id"]
    return tenant_value if isinstance(tenant_value, UUID) else UUID(str(tenant_value))


def _app_client_secret() -> str:
    """The app ``client_secret`` Shopify signs app-delivered webhooks with.

    VT-422 GAP-2: app-registered webhooks are signed with SHOPIFY_API_SECRET (the
    app secret), NOT the per-tenant push_secret. Raise 503 if unconfigured — a
    misconfigured app must NOT silently accept unverifiable deliveries.
    """
    secret = os.environ.get("SHOPIFY_API_SECRET", "")
    if not secret:
        logger.error("shopify webhook: SHOPIFY_API_SECRET unset — cannot verify HMAC")
        raise HTTPException(status_code=503, detail="Shopify app secret not configured")
    return secret


@router.post("/api/orchestrator/integrations/shopify/webhook")
async def shopify_webhook(
    request: Request,
    x_shopify_shop_domain: str = Header(default="", alias="X-Shopify-Shop-Domain"),
    x_shopify_topic: str = Header(default="", alias="X-Shopify-Topic"),
    x_shopify_hmac_sha256: str = Header(default="", alias="X-Shopify-Hmac-Sha256"),
) -> dict[str, Any]:
    # VT-422 GAP-2: real app-delivery shape — shop domain + app-secret HMAC. No
    # X-Viabe-Tenant header is sent by Shopify; tenant comes from the shop domain.
    if not x_shopify_shop_domain:
        raise HTTPException(
            status_code=400, detail="X-Shopify-Shop-Domain header required"
        )
    if not x_shopify_hmac_sha256:
        raise HTTPException(
            status_code=400, detail="X-Shopify-Hmac-Sha256 header required"
        )

    # (1) Verify HMAC against the APP client_secret BEFORE any DB work, so a forged
    # request never drives a tenant lookup. base64 over the RAW body.
    body = await request.body()
    app_secret = _app_client_secret()
    if not ShopifyConnector.verify_push_signature(
        body, dict(request.headers), app_secret
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    # (2) Resolve the tenant from the verified shop domain.
    tenant_uuid = _resolve_tenant_from_shop(x_shopify_shop_domain.strip().lower())

    # ---- checkouts/* : DEFERRED (drop-off substrate, separate feature) ----
    if x_shopify_topic.startswith("checkouts/"):
        logger.info(
            "shopify webhook checkouts topic DEFERRED (VT-417): tenant=%s topic=%s",
            tenant_uuid, x_shopify_topic,
        )
        return {"status": "ok", "topic": x_shopify_topic, "deferred": "checkouts"}

    import json as _json

    payload = cast("dict[str, Any]", _json.loads(body.decode("utf-8")))

    # ---- orders/paid : ATTRIBUTION-ONLY (no substrate write) ----
    if x_shopify_topic == "orders/paid":
        # An order completed; a future VT-N joins against sales_recovery_campaigns
        # to mark the outcome. Counts-only logging (CL-390) — never the amount.
        logger.info(
            "shopify orders/paid attribution candidate: tenant=%s", tenant_uuid
        )
        return {
            "status": "ok",
            "topic": x_shopify_topic,
            "attribution_hits": 1,
        }

    # ---- orders/create : SALE-OF-RECORD → real customers + sale ledger ----
    if x_shopify_topic == "orders/create":
        mapped = shopify_order_to_canonical(payload)
        if mapped.row is None:
            # No identity anchor (no phone / email / name) — nothing to land.
            return {
                "status": "ok",
                "topic": x_shopify_topic,
                "rows_committed": 0,
                "no_anchor": True,
            }
        summary = ingest_customer_rows(
            tenant_uuid, [mapped.row], acquired_via=_ACQUIRED_VIA
        )
        return {
            "status": "ok",
            "topic": x_shopify_topic,
            "rows_committed": summary.committed,
            "sales_written": summary.sales_written,
            "sales_skipped_duplicate": summary.sales_skipped_duplicate,
            "ambiguous": summary.ambiguous,
            "skipped_non_inr": int(mapped.skipped_non_inr),
        }

    # Unknown topic — acknowledge (2xx so Shopify stops retrying) but no-op.
    logger.info(
        "shopify webhook unhandled topic: tenant=%s topic=%s",
        tenant_uuid, x_shopify_topic,
    )
    return {"status": "ok", "topic": x_shopify_topic, "unhandled": True}
