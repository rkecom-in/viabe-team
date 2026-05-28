"""VT-208 Shopify webhook router.

Endpoint: ``POST /api/orchestrator/integrations/shopify/webhook``.

Receives 4 topics: ``checkouts/create``, ``checkouts/update``,
``orders/create``, ``orders/paid``. Verifies the HMAC SHA-256 signature
on the raw body via ``ShopifyConnector.verify_push_signature``. On
valid payloads:

- ``checkouts/*`` → field-mapped + deduped + persisted as drop_off rows
- ``orders/paid`` → attribution match (Sales Recovery outcome ping)

Per CL-72 the handler returns 2xx whenever possible; 403 on bad
signature is the one allowed non-2xx so Shopify stops retrying.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orchestrator.graph import get_pool
from orchestrator.integrations.connectors.shopify import ShopifyConnector
from orchestrator.integrations.dedupe import dedupe_customer_row

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/orchestrator/integrations/shopify/webhook")
async def shopify_webhook(
    request: Request,
    x_viabe_tenant: str = Header(default="", alias="X-Viabe-Tenant"),
    x_shopify_topic: str = Header(default="", alias="X-Shopify-Topic"),
    x_shopify_hmac_sha256: str = Header(default="", alias="X-Shopify-Hmac-Sha256"),
) -> dict[str, Any]:
    if not x_viabe_tenant:
        raise HTTPException(
            status_code=400, detail="X-Viabe-Tenant header required"
        )
    if not x_shopify_hmac_sha256:
        raise HTTPException(
            status_code=400, detail="X-Shopify-Hmac-Sha256 header required"
        )
    try:
        tenant_uuid = UUID(x_viabe_tenant)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from None

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT push_secret FROM tenant_oauth_tokens "
            "WHERE tenant_id = %s AND connector_id = 'shopify'",
            (str(tenant_uuid),),
        )
        raw = cur.fetchone()
    row = cast("dict[str, Any] | None", raw)
    if row is None or not row["push_secret"]:
        raise HTTPException(
            status_code=403, detail="no push_secret for tenant"
        )

    body = await request.body()
    if not ShopifyConnector.verify_push_signature(
        body, dict(request.headers), row["push_secret"]
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    canonical_rows = ShopifyConnector.parse_push_payload(body)
    persisted = 0
    attribution_hits = 0
    for canonical_row in canonical_rows:
        phone = canonical_row.get("phone")
        if not phone:
            continue
        if x_shopify_topic == "orders/paid":
            # Attribution: an order completed; future VT-N row joins
            # against drop_off rows + sales_recovery_campaigns to mark
            # the outcome. Phase-1 logs the match intent.
            logger.info(
                "shopify orders/paid attribution candidate: tenant=%s phone=%s "
                "amount=%s",
                tenant_uuid,
                phone,
                canonical_row.get("order_amount"),
            )
            attribution_hits += 1
        dedupe_customer_row(
            tenant_id=tenant_uuid,
            phone_e164=str(phone),
            connector_id="shopify",
            canonical_row=canonical_row,
        )
        persisted += 1

    return {
        "status": "ok",
        "topic": x_shopify_topic,
        "rows_persisted": persisted,
        "attribution_hits": attribution_hits,
    }
