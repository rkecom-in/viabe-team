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

SECURITY — state is NOT yet CSRF-hardened (Phase-1; FLAGGED for Cowork ruling)
-----------------------------------------------------------------------------
``state`` carries the raw ``tenant_id`` and the callback trusts it. The HMAC
proves the redirect came THROUGH Shopify, but NOT that *we* initiated this
install for this tenant. An attacker can build their own authorize URL with
``state=<victim_tenant>`` + their own ``*.myshopify.com`` shop, approve it, and
the callback would bind THEIR shop token under the victim's tenant row
(``tenant_oauth_tokens`` UPSERT) — an OAuth account-linking CSRF / tenant-
integration hijack.

This MIRRORS the existing Phase-1 google_sheet OAuth flow (api/oauth_callback.py),
whose docstring already notes "Production deployments should sign + verify state
to prevent CSRF; Phase-1 stores it raw." The proper fix is cross-cutting (affects
both connectors) and is a Cowork/Clau architectural decision, not an in-task
change:
  * ``/setup`` authenticates the caller (team-web owner session / internal
    secret) so only the tenant's owner can initiate an install, and mints a
    single-use, expiring ``state`` nonce stored server-side bound to
    (tenant_id, owner, shop).
  * the callback looks the nonce up, verifies unused + unexpired, and derives
    ``tenant_id`` from the STORED record — never from the URL.

Current mitigations until that lands: CL-422 (dev = synthetic only, NO real
merchant data until VT-231 Mumbai) + the live OAuth-install walk is E2E-deferred
(this path is not exercised against a real merchant store pre-E2E). Hardening is
rostered — do NOT onboard a real merchant on this path until it lands.

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
    # SECURITY (see module docstring): state is the raw tenant_id and is trusted
    # as the tenant identity — NOT yet CSRF-hardened (Phase-1, mirrors
    # google_sheet; FLAGGED for Cowork ruling). HMAC below proves Shopify origin,
    # not that we initiated this install for this tenant.
    tenant_uuid = _tenant_uuid(state)

    # Cowork #4: verify Shopify's HMAC over the FULL query (sans hmac) before
    # trusting any of it — prevents forged (non-Shopify) callbacks.
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
