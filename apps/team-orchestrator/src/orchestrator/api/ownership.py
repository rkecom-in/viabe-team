"""VT-411 — owner identity-binding endpoints (tier-2, on top of gstin_verified → owner_channel_verified).
Internal-secret gated (team-web proxies; the Twilio-Verify / MCA vendor calls all happen
orchestrator-side, never in team-web). Wires the DORMANT VT-411 ownership functions onto the critical
path. Two independent paths, either of which flips ``owner_channel_verified``:

- POST /api/orchestrator/onboard/ownership/otp/start {tenant_id, public_phone} → start a DISTINCT
  ownership-OTP to the DISCOVERED public business number → {verification_sid, status}.
- POST /api/orchestrator/onboard/ownership/otp/confirm {tenant_id, public_phone, code} → check the
  ownership-OTP; on APPROVAL sets owner_channel_verified → {owner_channel_verified: bool}.
- POST /api/orchestrator/onboard/ownership/din {tenant_id, din, cin, reason} → DIN-KYC: the DIN directs
  the verified company's CIN (MCA Director Master Data) ⇒ owner_channel_verified → {owner_channel_verified}.

Fail-closed: no path silently verifies (CL-390 — phone/DIN/code never logged here).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.auth.otp_rate_limit import check_otp_rate_limit
from orchestrator.auth.twilio_verify import TwilioVerifyError

logger = logging.getLogger(__name__)
router = APIRouter()


def _twilio_rest_exc() -> type[BaseException]:
    """The Twilio SDK's raw REST error (a real expired/consumed verification raises THIS, not a
    TwilioVerifyError subclass — twilio_verify does not wrap the SDK call). Imported lazily so the
    dep-less smoke env (no twilio package) never trips on this module's import."""
    try:
        from twilio.base.exceptions import TwilioRestException

        return TwilioRestException
    except Exception:  # noqa: BLE001 — SDK absent in dep-less env; nothing to catch then.
        return TwilioVerifyError


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


class OwnershipOtpStartBody(BaseModel):
    tenant_id: str
    public_phone: str


class OwnershipOtpConfirmBody(BaseModel):
    tenant_id: str
    public_phone: str
    code: str


class OwnershipDinBody(BaseModel):
    tenant_id: str
    din: str
    cin: str
    reason: str


@router.post("/api/orchestrator/onboard/ownership/otp/start")
def ownership_otp_start(
    body: OwnershipOtpStartBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_forwarded_for: str | None = Header(default=None, alias="X-Forwarded-For"),
) -> dict[str, Any]:
    """Start the DISTINCT ownership-OTP to the DISCOVERED public number (proves control of the
    registry/GBP-listed number — the ownership bind), observably, even when that number equals the
    signup number. ``public_phone`` MUST be E.164."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    # #14 — per-IP + per-phone OTP cap BEFORE the Twilio Verify call, mirroring the signup/owner-verify
    # OTP sibling (VT-394). The client IP is forwarded by the internal-secret-authed team-web proxy
    # (X-Forwarded-For); trusted ONLY past the secret check above. Fail-OPEN lives inside the limiter.
    # PII-safe: the limiter hashes IP + phone (CL-390); public_phone is never logged.
    client_ip = (x_forwarded_for or "").split(",")[0].strip() or "unknown"
    rl = check_otp_rate_limit(client_ip, body.public_phone)
    if not rl.allowed:
        logger.warning("[ownership-otp-start] rate limited (blocked_by=%s)", rl.blocked_by)
        raise HTTPException(status_code=429, detail={"code": "rate_limited"})

    from orchestrator.onboarding import ownership

    # #15 — a vendor/config failure must surface as a structured RETRYABLE error (502), never an
    # unhandled 500 the UI can't render. Catch BOTH our wrapper error AND the SDK's raw REST error.
    try:
        res = ownership.start_ownership_otp(body.tenant_id, body.public_phone)
    except (TwilioVerifyError, _twilio_rest_exc()) as exc:
        logger.warning("[ownership-otp-start] verify vendor failure: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail={"code": "verify_unavailable"}) from None
    return {"verification_sid": res.verification_sid, "status": res.status}


@router.post("/api/orchestrator/onboard/ownership/otp/confirm")
def ownership_otp_confirm(
    body: OwnershipOtpConfirmBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """Check the ownership-OTP; on APPROVAL set owner_channel_verified. Fail-closed otherwise."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    from orchestrator.onboarding import ownership

    # #15 — a vendor/config failure (incl. the SDK's raw TwilioRestException on an expired/consumed
    # verification) must surface as a distinct RETRYABLE error (502), NOT a 500 the UI mislabels as
    # "code invalid". Fail-closed: an exception here NEVER returns owner_channel_verified=True.
    try:
        verified = ownership.confirm_ownership_otp(body.tenant_id, body.public_phone, body.code)
    except (TwilioVerifyError, _twilio_rest_exc()) as exc:
        logger.warning("[ownership-otp-confirm] verify vendor failure: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail={"code": "verify_unavailable"}) from None
    return {"owner_channel_verified": verified}


@router.post("/api/orchestrator/onboard/ownership/din")
def ownership_din(
    body: OwnershipDinBody,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    """DIN-KYC: returns owner_channel_verified IFF the DIN is a registered director of ``cin`` (MCA
    Director Master Data). Fail-closed on any miss / vendor failure. The ``reason`` is the MCA
    purpose-of-access string and MUST be >= 20 chars."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    # VT-411 DIN-KYC PARKED with MCA (Fazal 2026-06-27): off by default (Sandbox MCA gov 504s) — ownership
    # rides the public-number OTP only. Return disabled (not an error) so team-web hides the DIN affordance.
    from orchestrator.feature_flags import sandbox_mca_enabled

    if not sandbox_mca_enabled():
        return {"owner_channel_verified": False, "disabled": True}
    if len(body.reason.strip()) < 20:
        raise HTTPException(status_code=422, detail={"code": "reason_too_short"})

    from orchestrator.onboarding import ownership

    verified = ownership.verify_owner_via_din(
        body.tenant_id, body.din, body.cin, reason=body.reason
    )
    return {"owner_channel_verified": verified}
