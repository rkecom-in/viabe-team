"""VT-250 — owner-portal OTP verify proxy (Twilio Verify).

Endpoints (team-web ↔ orchestrator internal trust boundary):
  - ``POST /api/orchestrator/owner/verify-start``  → start an OTP verification
  - ``POST /api/orchestrator/owner/verify-check``  → check an entered code

Auth: ``X-Internal-Secret`` (CL-72 internal API secret) — same boundary as
``ops_resolve``. team-web is the only caller; it owns the phone→tenant
resolution + session minting. This route is a thin proxy to
``orchestrator.auth.twilio_verify`` so the Twilio creds + Verify Service SID
stay in the orchestrator process (defense-in-depth, mirrors the resolve-phone
key-isolation pattern).

CL-390 (LOCKED): the request body carries the phone (+ code on check) but the
orchestrator NEVER logs them — only verification_sid + tenant_id reach a log
line (enforced inside twilio_verify). The response is PII-safe: verification
status + verification_sid only, never the phone or code echoed back.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.auth.twilio_verify import (
    ChannelGatedError,
    InvalidChannelError,
    TwilioVerifyError,
    check_verification,
    start_verification,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class VerifyStartBody(BaseModel):
    phone: str
    channel: str = "whatsapp"
    tenant_id: str | None = None


class VerifyCheckBody(BaseModel):
    phone: str
    code: str
    tenant_id: str | None = None


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


@router.post("/api/orchestrator/owner/verify-start")
def verify_start(
    body: VerifyStartBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        result = start_verification(
            body.phone, body.channel, tenant_id=body.tenant_id
        )
    except (ChannelGatedError, InvalidChannelError) as exc:
        # Caller error (bad/gated channel) — 400, PII-safe message.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TwilioVerifyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {
        "verification_sid": result.verification_sid,
        "status": result.status,
        "channel": result.channel,
    }


@router.post("/api/orchestrator/owner/verify-check")
def verify_check(
    body: VerifyCheckBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="X-Internal-Secret mismatch")
    try:
        result = check_verification(body.phone, body.code, tenant_id=body.tenant_id)
    except TwilioVerifyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {
        "verification_sid": result.verification_sid,
        "status": result.status,
        "approved": result.approved,
    }
