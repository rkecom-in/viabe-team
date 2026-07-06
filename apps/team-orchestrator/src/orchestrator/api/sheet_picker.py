"""VT-608 ruling 2 — the Google Sheets picker's backend (a minimal team-web page is the frontend;
this is what it calls). WA-in-app-browser link-out per CL-443: after OAuth, the owner taps a link
that opens this picker in the WA in-app browser, selects a spreadsheet + tab, and the selection
POSTs back here — persisted to ``tenant_integration_state`` (pending_owner_input + phase), then the
chat resume (the runner gate or the loop's own integration_agent dispatch) picks up from there.

INTERNAL_API_SECRET-guarded exactly like ``oauth_callback.py``'s own ``/google_sheet/setup`` — team-
web calls these server-side after authenticating the owner session, passing the verified tenant_id
(never trusted from an unauthenticated client). No manual credential paste (CL-421); no raw sheet
row content ever passes through these endpoints (list/select only — sample pull is the agent tool's
own job, COUNTS-only to the LLM).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.api.oauth_callback import _verify_internal_secret
from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
from orchestrator.onboarding.shopify_onboarding import (
    PHASE_SAMPLE,
    _validated_pending,
    _write_state,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_CONNECTOR_ID = "google_sheet"


class SpreadsheetListResponse(BaseModel):
    spreadsheets: list[dict[str, str]]


class TabListResponse(BaseModel):
    tabs: list[str]


class SheetSelectionBody(BaseModel):
    tenant_id: str
    spreadsheet_id: str
    tab_name: str


class SheetSelectionResponse(BaseModel):
    accepted: bool
    phase: str


def _require_tenant(tenant_id: str) -> UUID:
    try:
        return UUID(tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"tenant_id must be a UUID; got {tenant_id!r}"
        ) from None


@router.get("/api/orchestrator/integrations/google_sheet/spreadsheets")
def list_spreadsheets(
    tenant_id: str,
    x_internal_secret: str | None = Header(default=None),
) -> SpreadsheetListResponse:
    """List the owner's spreadsheets for the picker page. Requires a completed OAuth
    (raises 502 if no token — the picker page shouldn't be reachable before OAuth
    completes, but this is the fail-closed backstop, not a silent empty list)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_uuid = _require_tenant(tenant_id)
    try:
        files = GoogleSheetConnector().list_spreadsheets(tenant_uuid)
    except Exception as exc:  # noqa: BLE001 — surface as a clean 502, never a raw 500 traceback
        logger.warning("VT-608 list_spreadsheets failed tenant=%s: %s", tenant_uuid, exc)
        raise HTTPException(status_code=502, detail="could not list spreadsheets") from exc
    return SpreadsheetListResponse(spreadsheets=files)


@router.get("/api/orchestrator/integrations/google_sheet/tabs")
def list_tabs(
    tenant_id: str,
    spreadsheet_id: str,
    x_internal_secret: str | None = Header(default=None),
) -> TabListResponse:
    """List a spreadsheet's tab names, once the owner picked a spreadsheet."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_uuid = _require_tenant(tenant_id)
    try:
        tabs = GoogleSheetConnector().list_tabs(tenant_uuid, spreadsheet_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VT-608 list_tabs failed tenant=%s spreadsheet=%s: %s", tenant_uuid, spreadsheet_id, exc
        )
        raise HTTPException(status_code=502, detail="could not list tabs") from exc
    return TabListResponse(tabs=tabs)


@router.post("/api/orchestrator/integrations/google_sheet/select")
def select_spreadsheet(
    body: SheetSelectionBody,
    x_internal_secret: str | None = Header(default=None),
) -> SheetSelectionResponse:
    """Persist the owner's spreadsheet+tab selection and advance the phase to
    phase_3_sample_pull — the chat resume (runner gate or the loop's integration_agent dispatch)
    picks up from there and calls pull_sample."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_uuid = _require_tenant(body.tenant_id)
    if not body.spreadsheet_id or not body.tab_name:
        raise HTTPException(status_code=400, detail="spreadsheet_id and tab_name are required")

    pending = _validated_pending(
        awaiting="sample_pull_pending",  # VT-608 — a machine waypoint, not an owner question
        prompt_text="Spreadsheet selected — pulling a sample now.",
        connector_id=_CONNECTOR_ID,
        metadata={"spreadsheet_id": body.spreadsheet_id, "tab_name": body.tab_name},
    )
    _write_state(tenant_uuid, phase=PHASE_SAMPLE, connector_id=_CONNECTOR_ID, pending=pending)
    logger.info(
        "VT-608 sheet selection persisted tenant=%s phase=%s", tenant_uuid, PHASE_SAMPLE
    )
    return SheetSelectionResponse(accepted=True, phase=PHASE_SAMPLE)


__all__ = ["router"]
