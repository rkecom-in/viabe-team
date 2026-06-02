"""VT-286 — WABA Embedded Signup router (VT-289-hardened).

  POST /api/orchestrator/integrations/whatsapp/setup   (body: tenant_id, display_name?)
      INTERNAL_API_SECRET-guarded (team-web calls server-side after authenticating the
      owner session). Mints a VT-289 state nonce + returns the Meta Embedded Signup URL
      as JSON; team-web 302s the browser to the owner's ~5-min popup.

  GET /api/orchestrator/integrations/whatsapp/embedded-callback?code=&state=
      Meta's redirect target. Claims the VT-289 nonce (single-use, connector-matched),
      derives tenant from the STORED record (never the URL), then exchanges the code →
      WABA token + provisions a dedicated number → persists at status='verifying'.

Owner-owned WABA (Meta client-owned mandate). Zero-paste after approve (CL-421).
# live Embedded-Signup walk deferred to E2E (Fazal 2026-06-02) — Tech Provider track
# initiated separately; build canaried against injected/sandbox. See launch-tracker.
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from orchestrator.integrations.oauth_state import (
    claim_install_state,
    mint_install_state,
)
from orchestrator.integrations.whatsapp_account import (
    WhatsAppConfigError,
    build_embedded_signup_url,
    connect_waba,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_CONNECTOR_ID = "whatsapp"


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


class WhatsAppSetupBody(BaseModel):
    tenant_id: str
    display_name: str | None = None


class WhatsAppSetupResponse(BaseModel):
    embedded_signup_url: str


class WhatsAppCallbackResponse(BaseModel):
    status: str
    waba_id: str | None
    phone_number: str | None
    waba_status: str


@router.post("/api/orchestrator/integrations/whatsapp/setup")
def whatsapp_setup(
    body: WhatsAppSetupBody,
    x_internal_secret: str | None = Header(default=None),
) -> WhatsAppSetupResponse:
    """Start WABA Embedded Signup. team-web (owner-authenticated) calls this with the
    internal secret; we mint a VT-289 nonce + return the Meta ES URL as JSON."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_uuid = _tenant_uuid(body.tenant_id)
    try:
        # carry display_name through the nonce's target so the callback can persist it.
        target = body.display_name or None
        state = mint_install_state(tenant_uuid, _CONNECTOR_ID, target=target)
        url = build_embedded_signup_url(tenant_uuid, state)
    except WhatsAppConfigError as exc:
        logger.error("VT-286 whatsapp_setup misconfig: %s", exc)
        raise HTTPException(status_code=503, detail="WhatsApp OAuth not configured") from exc
    return WhatsAppSetupResponse(embedded_signup_url=url)


@router.get("/api/orchestrator/integrations/whatsapp/embedded-callback")
def whatsapp_embedded_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> WhatsAppCallbackResponse:
    """Meta ES redirect target. VT-289: claim the nonce, derive tenant from the stored
    record (NEVER the URL), then exchange + provision + persist."""
    claimed = claim_install_state(state, _CONNECTOR_ID)
    if claimed is None:
        logger.warning("VT-286 callback: state claim rejected (forged/used/expired)")
        raise HTTPException(status_code=401, detail="invalid or expired state")
    tenant_uuid = claimed.tenant_id
    display_name = claimed.target  # minted at /setup
    try:
        account = connect_waba(tenant_uuid, code, display_name=display_name)
    except Exception as exc:
        logger.exception(
            "VT-286 WABA connect failed",
            extra={"tenant_id": str(tenant_uuid), "code_prefix": code[:8]},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return WhatsAppCallbackResponse(
        status="ok",
        waba_id=account.waba_id,
        phone_number=account.phone_number,
        waba_status=account.status,
    )
