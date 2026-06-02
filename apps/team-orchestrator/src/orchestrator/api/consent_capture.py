"""VT-8.5 — consent-capture endpoint.

POST /api/orchestrator/consent/capture
POST /api/orchestrator/consent/opt-out

Called by team-web's customer-facing QR opt-in page after the customer submits
their phone + accepts the terms. team-web identifies the tenant from the
(signed) QR token and forwards phone + the ``consent_text_version`` the customer
agreed to. Guarded by ``INTERNAL_API_SECRET`` (team-web is the trusted caller;
QR-token validation happens in team-web — same trust model as onboard_step).

Privacy: the raw phone is tokenised INSIDE ``record_consent`` (CL-390) — it is
accepted over the wire only to tokenise, never persisted raw, never logged.
CL-422: dev = synthetic only until VT-231 (prod Mumbai).
"""

from __future__ import annotations

import hmac
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.privacy import consent as consent_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _parse_tenant(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from exc


class ConsentCaptureBody(BaseModel):
    tenant_id: str
    phone_e164: str
    consent_text_version: str
    consent_method: str = "qr_optin"
    source: str | None = None
    locale: str | None = None


class ConsentCaptureResponse(BaseModel):
    recorded: bool
    active: bool
    phone_token: str
    consent_text_version: str


class ConsentOptOutBody(BaseModel):
    tenant_id: str
    phone_e164: str


class ConsentOptOutResponse(BaseModel):
    opted_out: bool


@router.post("/api/orchestrator/consent/capture")
async def consent_capture(
    body: ConsentCaptureBody,
    x_internal_secret: str | None = Header(default=None),
) -> ConsentCaptureResponse:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_id = _parse_tenant(body.tenant_id)
    rec = consent_service.record_consent(
        tenant_id,
        body.phone_e164,
        consent_text_version=body.consent_text_version,
        consent_method=body.consent_method,
        source=body.source,
        locale=body.locale,
    )
    return ConsentCaptureResponse(
        recorded=True,
        active=rec.active,
        phone_token=rec.phone_token,
        consent_text_version=rec.consent_text_version,
    )


@router.post("/api/orchestrator/consent/opt-out")
async def consent_opt_out(
    body: ConsentOptOutBody,
    x_internal_secret: str | None = Header(default=None),
) -> ConsentOptOutResponse:
    """Customer-facing consent withdrawal. Inbound trigger for the ``opt_out``
    writer (Fix 3); a WhatsApp-inbound customer-STOP path is a rostered
    follow-up (VT-251 campaigns, post-launch)."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    tenant_id = _parse_tenant(body.tenant_id)
    changed = consent_service.opt_out_for_phone(tenant_id, body.phone_e164)
    return ConsentOptOutResponse(opted_out=changed)
