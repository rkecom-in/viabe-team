"""VT-208 / VT-417 Shopify webhook router.

Endpoint: ``POST /api/orchestrator/integrations/shopify/webhook``.

Receives 4 topics: ``checkouts/create``, ``checkouts/update``,
``orders/create``, ``orders/paid``. Verifies the HMAC SHA-256 signature
on the raw body via ``ShopifyConnector.verify_push_signature``.

VT-417 — the inbound lineage now writes the REAL customer substrate (it used
to terminate at the Phase-1 ``dedupe_customer_row`` stub, which wrote only a
phone-token and discarded the order). Topic routing:

- ``orders/create`` → SALE-OF-RECORD. Map the order → ``CanonicalRow`` →
  ``ingest_customer_rows(acquired_via="shopify")`` → a real ``customers`` row +
  ONE ``sale`` ``customer_ledger_entries`` row (idempotent on re-delivery via the
  ledger ``entry_key``). Consent is NEVER written from Shopify (option A, §2.4):
  the detector's consent AND-gate keeps the customer out of lapsed candidates
  until they opt in via the WhatsApp/QR path.
- ``orders/paid`` → ATTRIBUTION-ONLY. An order completed; logs the match intent
  for the future Sales-Recovery outcome ping. NO second ledger write (writing on
  create AND paid would idempotently collapse on ``entry_key``, but we don't rely
  on that — ``orders/create`` is the single writer).
- ``checkouts/*`` → DEFERRED (abandoned-checkout / drop-off is a different
  substrate, a separate feature). Explicitly acknowledged, not silently dropped.

Per CL-72 the handler returns 2xx whenever possible; 403 on bad signature is the
one allowed non-2xx so Shopify stops retrying.
"""

from __future__ import annotations

import logging
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
