"""VT-207 OAuth callback router.

Endpoint: ``GET /api/orchestrator/integrations/oauth/callback``.

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

from orchestrator.integrations.connectors.google_sheet import (
    GoogleSheetConnector,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/orchestrator/integrations/oauth/callback")
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
