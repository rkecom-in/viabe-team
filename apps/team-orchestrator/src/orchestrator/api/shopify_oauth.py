"""VT-283 — Shopify OWNER-FACING OAuth managed-install router. HARDENED by VT-289.

Two endpoints, mirroring the hardened google_sheet flow (api/oauth_callback.py):

  POST /api/orchestrator/integrations/shopify/setup   (body: tenant_id, shop)
      INTERNAL_API_SECRET-guarded (team-web calls server-side after authenticating
      the owner session). Validates the shop domain, mints a single-use VT-289
      ``state`` nonce bound to (tenant, 'shopify', shop), and returns the authorize
      URL as JSON; team-web 302s the browser to the merchant's consent screen.

  GET /api/orchestrator/integrations/shopify/oauth/callback?code=&shop=&hmac=&state=
      Shopify's redirect target. Two mandatory checks BEFORE the code-exchange:
      (1) HMAC over the query verifies (Shopify origin), and (2) the VT-289 nonce
      claims successfully (single-use, unexpired, connector-matched). The tenant_id
      is derived from the STORED nonce record — NEVER from the URL ``state`` — which
      defeats the account-linking CSRF (an attacker's forged state was never minted
      by us, so the claim fails). The shop is likewise taken from the minted record.

This is the PRODUCTION zero-paste path for real merchants (different org);
client_credentials stays the dev/own-store path. CL-421 / CL-427.

# live OAuth-install walk deferred to E2E (Fazal 2026-06-02): cannot be live-
# walked on our own dev store (same-org = client_credentials). Real-merchant
# walk happens during end-to-end testing — see .viabe/launch-tracker.md.
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from orchestrator.integrations.connectors.shopify import (
    ShopDomainError,
    ShopifyConfigError,
    ShopifyConnector,
    validate_shop_domain,
    verify_oauth_hmac,
)
from orchestrator.integrations.oauth_state import (
    claim_install_state,
    mint_install_state,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_CONNECTOR_ID = "shopify"


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _tenant_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"tenant_id must be a UUID; got {value!r}"
        ) from None


class ShopifySetupBody(BaseModel):
    tenant_id: str
    shop: str


class ShopifySetupResponse(BaseModel):
    authorize_url: str


@router.post("/api/orchestrator/integrations/shopify/setup")
def shopify_setup(
    body: ShopifySetupBody,
    x_internal_secret: str | None = Header(default=None),
) -> ShopifySetupResponse:
    """Start the merchant OAuth install. team-web (owner-authenticated) calls this
    server-side with INTERNAL_API_SECRET; we mint a VT-289 nonce + return the
    authorize URL as JSON for team-web to 302 the browser to."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_uuid = _tenant_uuid(body.tenant_id)
    try:
        # validate the shop BEFORE minting / interpolation (Cowork #1).
        shop = validate_shop_domain(body.shop)
        state = mint_install_state(tenant_uuid, _CONNECTOR_ID, target=shop)
        auth_url = ShopifyConnector().build_oauth_install_url(
            tenant_uuid, shop, state=state
        )
    except ShopDomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ShopifyConfigError as exc:
        logger.error("VT-283 shopify_setup misconfig: %s", exc)
        raise HTTPException(status_code=503, detail="Shopify OAuth not configured") from exc
    return ShopifySetupResponse(authorize_url=auth_url)


@router.get("/api/orchestrator/integrations/shopify/oauth/callback")
def shopify_oauth_callback(
    request: Request,
    code: str = Query(...),
    shop: str = Query(...),
    state: str = Query(...),
) -> dict[str, str]:
    """OAuth redirect target. VT-289 hardened: verify HMAC (Shopify origin) + claim
    the nonce (we initiated it), derive tenant + shop from the STORED record, then
    exchange code → offline token."""
    # (1) HMAC over the full query (sans hmac) — proves Shopify origin. Done first
    # so a forged (non-Shopify) request never consumes the single-use nonce.
    client_secret = os.environ.get("SHOPIFY_API_SECRET", "")
    if not client_secret:
        logger.error("VT-283 callback: SHOPIFY_API_SECRET unset — cannot verify HMAC")
        raise HTTPException(status_code=503, detail="Shopify OAuth not configured")
    query = {k: v for k, v in request.query_params.items()}
    if not verify_oauth_hmac(query, client_secret):
        logger.warning("VT-283 callback: HMAC verification failed (shop=%s)", shop)
        raise HTTPException(status_code=401, detail="invalid HMAC")

    # (2) VT-289: claim the single-use nonce; tenant + shop come from the STORED
    # record, never the URL. A forged/replayed/expired state → reject.
    claimed = claim_install_state(state, _CONNECTOR_ID)
    if claimed is None:
        logger.warning("VT-283 callback: state claim rejected (forged/used/expired)")
        raise HTTPException(status_code=401, detail="invalid or expired state")
    tenant_uuid = claimed.tenant_id
    shop_from_record = claimed.target or shop  # authoritative shop = the minted one

    connector = ShopifyConnector()
    try:
        result = connector.complete_auth(
            tenant_uuid, {"code": code, "shop": shop_from_record}
        )
    except ShopDomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "VT-283 Shopify OAuth complete_auth failed",
            extra={"tenant_id": str(tenant_uuid), "code_prefix": code[:8]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # VT-422 (setup_push flow gap): register the webhooks on install. ``setup_push``
    # is defined on the connector but was NEVER called by this callback — so on a real
    # OAuth install the webhooks were never registered and ``orders/create`` never
    # delivered. Fire it AFTER complete_auth (the OAuth branch), once the offline token
    # + push_secret row exists. Registration failure does NOT fail the install (the token
    # is already stored, the merchant is connected); it is logged and surfaced as
    # ``webhooks_registered=false`` so the canary (GET webhooks.json) can flag it. A
    # subsequent re-run / scheduled re-register can recover.
    # VT-453 (fix-immediately): a webhook-registration miss must NOT silently leave the merchant
    # connected-but-uningested. Bounded retry on a transient failure, then surface an ACTIONABLE state
    # (the status is no longer a bare "ok", and action_required names the recovery) so a quiet miss is
    # visible to the caller / a re-register path, not hidden in a log line.
    webhooks_registered = False
    for _attempt in range(2):  # one bounded retry
        try:
            push = connector.setup_push(tenant_uuid)
            webhooks_registered = bool(push.get("topics"))
            if webhooks_registered:
                break
        except Exception:  # noqa: BLE001
            logger.warning(
                "VT-453 Shopify webhook registration attempt %d failed (token stored, merchant connected)",
                _attempt + 1, extra={"tenant_id": str(tenant_uuid)}, exc_info=True,
            )
    if not webhooks_registered:
        logger.error(
            "VT-453 Shopify webhooks NOT registered after retry — merchant connected but orders will NOT "
            "ingest until re-registered (tenant=%s)", str(tenant_uuid),
        )

    return {
        "status": "ok" if webhooks_registered else "connected_webhooks_unregistered",
        "connector_id": connector.connector_id,
        "mode": str(result.get("mode", "oauth_install")),
        "shop_url": str(result.get("shop_url", "")),
        "scopes": ",".join(result.get("scopes", [])),
        "webhooks_registered": str(webhooks_registered).lower(),
        "action_required": "" if webhooks_registered else "reregister_webhooks",
    }
