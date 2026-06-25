"""VT-411 — owner identity binding (tier-2, on top of gstin_verified → owner_channel_verified). Two
independent paths, either of which sets ``owner_channel_verified``:
  (a) DIN-KYC: the owner's DIN directs the verified company's CIN (MCA Director Master Data) ⇒ a
      registered director ⇒ KYC-grade ownership (the strongest signal).
  (b) ownership-OTP: a DISTINCT Twilio-Verify OTP to the DISCOVERED public business number (the GBP
      phone) — observably exercised as its own step even when it equals the signup number.

Fail-closed: no path silently verifies — only an asserted DIN↔CIN link or an APPROVED OTP flips the
flag. CL-390: phone/DIN are never logged here (only counts/tenant_id).
"""

from __future__ import annotations

import logging

from orchestrator.auth import twilio_verify

logger = logging.getLogger(__name__)


def verify_owner_via_din(
    tenant_id: str, din: str, company_cin: str, *, reason: str, request_fn: object | None = None
) -> bool:
    """(a) DIN-KYC. Returns True + sets owner_channel_verified IFF the DIN is a registered director of
    ``company_cin`` (per MCA Director Master Data). Fail-closed on any miss / vendor failure."""
    from orchestrator.integrations.methods.mca import director_master_data

    din = (din or "").strip()
    company_cin = (company_cin or "").strip()
    if not din or not company_cin:
        return False
    dmd = director_master_data(din, reason=reason, request_fn=request_fn)  # type: ignore[arg-type]
    if not dmd.ok or not dmd.directs_cin(company_cin):
        logger.info("vt411: DIN-KYC not asserted tenant=%s (no DIN-CIN link)", tenant_id)
        return False
    from orchestrator.onboarding.mca_store import set_owner_channel_verified

    set_owner_channel_verified(tenant_id)
    logger.info("vt411: owner_channel_verified via DIN-KYC tenant=%s", tenant_id)
    return True


def start_ownership_otp(
    tenant_id: str, public_phone: str, *, channel: str = twilio_verify.LIVE_CHANNEL
) -> twilio_verify.VerifyStartResult:
    """(b) Start the DISTINCT ownership-OTP to the DISCOVERED public number. Separate from the signup
    OTP — it proves control of the registry/GBP-listed number (the ownership bind), observably, even
    when that number equals the signup number. ``public_phone`` MUST be E.164."""
    return twilio_verify.start_verification(public_phone, channel, tenant_id=str(tenant_id))


def confirm_ownership_otp(
    tenant_id: str, public_phone: str, code: str, *, channel: str = twilio_verify.LIVE_CHANNEL
) -> bool:
    """(b) Check the ownership-OTP; on APPROVAL set owner_channel_verified. Fail-closed otherwise."""
    res = twilio_verify.check_verification(public_phone, code, tenant_id=str(tenant_id))
    if not res.approved:
        return False
    from orchestrator.onboarding.mca_store import set_owner_channel_verified

    set_owner_channel_verified(tenant_id)
    logger.info("vt411: owner_channel_verified via ownership-OTP tenant=%s", tenant_id)
    return True
