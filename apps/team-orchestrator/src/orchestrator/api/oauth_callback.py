"""VT-207 OAuth callback router (path aligned via VT-212 manual walk).

Endpoint: ``GET /api/orchestrator/integrations/google/callback``.

Path aligns with ``GOOGLE_OAUTH_REDIRECT_URI`` env var (was
``/oauth/callback`` which 404'd because Google redirected to the
env-configured path). VT-212 manual walk surfaced the mismatch.

Google redirects the owner here after they grant the OAuth scope.
Query carries ``code`` + ``state`` (tenant_id). The handler calls
``GoogleSheetConnector.complete_auth(...)`` to exchange the code for
a refresh_token (persisted encrypted via VT-191 Fernet substrate).

Per VT-205 substrate: only ``google_sheet`` connector wired today;
future connectors register their own callback handlers OR route
through a generic dispatcher (Phase-2).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from orchestrator.integrations.connectors.google_sheet import (
    GoogleSheetConnector,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/orchestrator/integrations/google_sheet/setup")
def google_sheet_setup(tenant_id: str = Query(...)) -> RedirectResponse:
    """Start the Google OAuth flow.

    Owner hits this URL → server builds the Google consent URL with the
    correct ``redirect_uri`` + scope + ``state=tenant_id`` → 302
    redirects the browser there. VT-212 manual walk uses this as the
    single entry point Cowork relays to Fazal.
    """
    try:
        tenant_uuid = UUID(tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"tenant_id must be a UUID; got {tenant_id!r}",
        ) from None
    auth_url = GoogleSheetConnector().build_auth_url(tenant_uuid)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/api/orchestrator/integrations/google/callback")
def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> dict[str, str]:
    """OAuth redirect target. Exchanges code → refresh_token → store."""
    try:
        tenant_uuid = UUID(state)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"state must be a tenant_id UUID; got {state!r}",
        ) from None
    connector = GoogleSheetConnector()
    try:
        result = connector.complete_auth(tenant_uuid, {"code": code})
    except Exception as exc:
        logger.exception(
            "VT-207 OAuth complete_auth failed",
            extra={"tenant_id": state, "code_prefix": code[:8]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "status": "ok",
        "connector_id": connector.connector_id,
        "scopes": ",".join(result.get("scopes", [])),
    }
