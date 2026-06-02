"""VT-283 — Shopify OWNER-FACING OAuth managed-install router.

Two endpoints, mirroring the google_sheet OAuth flow (api/oauth_callback.py):

  GET /api/orchestrator/integrations/shopify/setup?tenant_id=&shop=
      Owner-entered ``shop`` is validated, an authorize URL is built (state =
      tenant_id), and the browser is 302'd to the MERCHANT's consent screen.

  GET /api/orchestrator/integrations/shopify/oauth/callback?code=&shop=&hmac=&state=
      Shopify's redirect target. BEFORE the code-exchange, two checks are
      mandatory (Cowork VT-283 #4): the HMAC on the query verifies against the
      app secret, AND state == the resolved tenant_id. Only then is the code
      exchanged for an offline token and persisted.

This is the PRODUCTION zero-paste path for real merchants (different org);
client_credentials stays the dev/own-store path. CL-421 / CL-427.

# live OAuth-install walk deferred to E2E (Fazal 2026-06-02): cannot be live-
# walked on our own dev store (same-org = client_credentials). Real-merchant
# walk happens during end-to-end testing — see .viabe/launch-tracker.md.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from orchestrator.integrations.connectors.shopify import (
    ShopDomainError,
    ShopifyConfigError,
    ShopifyConnector,
    verify_oauth_hmac,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _tenant_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"tenant_id must be a UUID; got {value!r}"
        ) from None


@router.get("/api/orchestrator/integrations/shopify/setup")
def shopify_setup(
    tenant_id: str = Query(...),
    shop: str = Query(...),
) -> RedirectResponse:
    """Start the merchant OAuth install. 302s to Shopify's consent screen."""
    tenant_uuid = _tenant_uuid(tenant_id)
    try:
        auth_url = ShopifyConnector().build_oauth_install_url(tenant_uuid, shop)
    except ShopDomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ShopifyConfigError as exc:
        # Missing app creds / redirect URI is an operator misconfig, not owner error.
        logger.error("VT-283 shopify_setup misconfig: %s", exc)
        raise HTTPException(status_code=503, detail="Shopify OAuth not configured") from exc
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/api/orchestrator/integrations/shopify/oauth/callback")
def shopify_oauth_callback(
    request: Request,
    code: str = Query(...),
    shop: str = Query(...),
    state: str = Query(...),
) -> dict[str, str]:
    """OAuth redirect target. Verify HMAC + state, then exchange code → token."""
    tenant_uuid = _tenant_uuid(state)

    # Cowork #4: verify Shopify's HMAC over the FULL query (sans hmac) before
    # trusting any of it — prevents forged callbacks.
    client_secret = os.environ.get("SHOPIFY_API_SECRET", "")
    if not client_secret:
        logger.error("VT-283 callback: SHOPIFY_API_SECRET unset — cannot verify HMAC")
        raise HTTPException(status_code=503, detail="Shopify OAuth not configured")
    query = {k: v for k, v in request.query_params.items()}
    if not verify_oauth_hmac(query, client_secret):
        logger.warning("VT-283 callback: HMAC verification failed (shop=%s)", shop)
        raise HTTPException(status_code=401, detail="invalid HMAC")

    connector = ShopifyConnector()
    try:
        result = connector.complete_auth(tenant_uuid, {"code": code, "shop": shop})
    except ShopDomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "VT-283 Shopify OAuth complete_auth failed",
            extra={"tenant_id": state, "shop": shop, "code_prefix": code[:8]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "status": "ok",
        "connector_id": connector.connector_id,
        "mode": str(result.get("mode", "oauth_install")),
        "shop_url": str(result.get("shop_url", "")),
        "scopes": ",".join(result.get("scopes", [])),
    }
