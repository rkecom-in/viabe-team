"""VT-207 OAuth callback router — HARDENED by VT-289 (state-CSRF).

Endpoints:
- ``POST /api/orchestrator/integrations/google_sheet/setup`` — start the Google OAuth
  flow. Guarded by ``INTERNAL_API_SECRET`` (team-web calls it server-side AFTER
  authenticating the owner session, passing the verified tenant_id). Mints a single-use
  VT-289 ``state`` nonce and returns the authorize URL as JSON; team-web 302s the
  browser. The secret never touches the client.
- ``GET /api/orchestrator/integrations/google/callback`` — Google's redirect target.
  CLAIMS the VT-289 nonce (single-use, unexpired, connector-matched) and derives the
  tenant_id from the STORED record — NEVER from the URL ``state``. Then exchanges the
  code → refresh_token (persisted encrypted via VT-191 Fernet).

VT-289: the prior contract trusted ``state`` AS the tenant_id (account-linking CSRF —
an attacker forges ``state=<victim_tenant>``). The nonce defeats that: a state we never
minted fails the claim. Same hardening covers shopify (#227) + WhatsApp (VT-286).
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
from orchestrator.integrations.oauth_state import (
    claim_install_state,
    mint_install_state,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_CONNECTOR_ID = "google_sheet"


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class GoogleSheetSetupBody(BaseModel):
    tenant_id: str


class GoogleSheetSetupResponse(BaseModel):
    authorize_url: str


@router.post("/api/orchestrator/integrations/google_sheet/setup")
def google_sheet_setup(
    body: GoogleSheetSetupBody,
    x_internal_secret: str | None = Header(default=None),
) -> GoogleSheetSetupResponse:
    """Start Google OAuth. team-web (owner-authenticated) calls this server-side with
    INTERNAL_API_SECRET; we mint a VT-289 nonce and return the authorize URL as JSON
    for team-web to 302 the browser to."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        tenant_uuid = UUID(body.tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"tenant_id must be a UUID; got {body.tenant_id!r}"
        ) from None
    state = mint_install_state(tenant_uuid, _CONNECTOR_ID)
    auth_url = GoogleSheetConnector().build_auth_url(tenant_uuid, state=state)
    return GoogleSheetSetupResponse(authorize_url=auth_url)


@router.get("/api/orchestrator/integrations/google/callback")
def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> dict[str, str]:
    """OAuth redirect target. VT-289: claim the nonce, derive tenant from the stored
    record (NOT the URL), then exchange code → refresh_token → store."""
    claimed = claim_install_state(state, _CONNECTOR_ID)
    if claimed is None:
        # Unknown / already-used / expired / forged state — reject before any exchange.
        logger.warning("VT-289 google callback: state claim rejected")
        raise HTTPException(status_code=401, detail="invalid or expired state")
    tenant_uuid = claimed.tenant_id  # authoritative — from the stored nonce, not URL.
    connector = GoogleSheetConnector()
    try:
        result = connector.complete_auth(tenant_uuid, {"code": code})
    except Exception as exc:
        logger.exception(
            "VT-207 OAuth complete_auth failed",
            extra={"tenant_id": str(tenant_uuid), "code_prefix": code[:8]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "status": "ok",
        "connector_id": connector.connector_id,
        "scopes": ",".join(result.get("scopes", [])),
    }
