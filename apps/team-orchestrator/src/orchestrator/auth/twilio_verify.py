"""Twilio Verify client for the owner-portal OTP login flow (VT-250).

Owner enters their mobile in the team-web login surface → team-web calls the
orchestrator → this module starts a Twilio Verify verification (OTP delivery)
and later checks the entered code. The Verify Service SID is a Fazal-provisioned
env (``TWILIO_VERIFY_SERVICE_SID``) — Cowork ruling D2.

Channels (Cowork ruling D2):
  - ``whatsapp`` — the LIVE channel.
  - ``sms`` — built but GATED OFF until SMS DLT approval (Fazal). Requesting
    the sms channel raises ``ChannelGatedError`` unless the gate env
    ``VT250_SMS_CHANNEL_ENABLED=1`` is explicitly set.

Mock mode (mirrors ``twilio_send.TEAM_TWILIO_MOCK_MODE``): when
``TEAM_TWILIO_VERIFY_MOCK_MODE=1`` no network call is made. start→pending,
check(correct)→approved, check(anything-else)→denied. The "correct" code in
mock mode is ``VT250_MOCK_OTP`` (default ``123456``). Mock mode is the default
for tests + the canary; the real Verify path activates only when the flag is
absent AND a real Service SID is present.

CL-390 (LOCKED): NEVER log the phone number or the OTP code. Log lines carry
ONLY ``verification_sid`` + ``tenant_id`` (+ channel/status). The plaintext
phone and code never reach a log line, an exception message we emit, or a
returned field beyond the opaque verification_sid.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# Twilio Verify channel literals. whatsapp = live; sms = built, gated OFF.
LIVE_CHANNEL = "whatsapp"
GATED_CHANNEL = "sms"
_VALID_CHANNELS = (LIVE_CHANNEL, GATED_CHANNEL)


class TwilioVerifyError(RuntimeError):
    """Base error for the Verify client (config / API problems)."""


class VerifyServiceNotConfigured(TwilioVerifyError):
    """Raised when TWILIO_VERIFY_SERVICE_SID is required but absent."""


class ChannelGatedError(TwilioVerifyError):
    """Raised when the sms channel is requested but not gate-enabled.

    SMS is built but OFF until DLT approval (Cowork D2). The error message
    is PII-safe — it names the channel only, never a phone number.
    """


class InvalidChannelError(TwilioVerifyError):
    """Raised when channel is not one of the known Verify channels."""


@dataclass(frozen=True)
class VerifyStartResult:
    """Outcome of starting a verification. PII-safe — no phone, no code."""

    verification_sid: str
    status: str  # 'pending' on success
    channel: str


@dataclass(frozen=True)
class VerifyCheckResult:
    """Outcome of checking a code. PII-safe — no phone, no code.

    ``approved`` is True only when Twilio returns status == 'approved'.
    Any other status (pending / denied / max-attempts / expired) → approved
    False; the raw status is surfaced for the caller's audit (never the code).
    """

    verification_sid: str | None
    status: str  # 'approved' | 'denied' | 'pending' | ...
    approved: bool


def _mock_mode() -> bool:
    return os.environ.get("TEAM_TWILIO_VERIFY_MOCK_MODE", "0") == "1"


def _sms_gate_open() -> bool:
    """SMS channel is OFF by default; open only with the explicit env gate."""
    return os.environ.get("VT250_SMS_CHANNEL_ENABLED", "0") == "1"


def _mock_otp() -> str:
    return os.environ.get("VT250_MOCK_OTP", "123456")


def _validate_channel(channel: str) -> None:
    if channel not in _VALID_CHANNELS:
        raise InvalidChannelError(
            f"unknown Verify channel '{channel}' "
            f"(valid: {', '.join(_VALID_CHANNELS)})"
        )
    if channel == GATED_CHANNEL and not _sms_gate_open():
        raise ChannelGatedError(
            "sms channel is gated OFF (SMS DLT pending). "
            "Set VT250_SMS_CHANNEL_ENABLED=1 to enable; whatsapp is the "
            "live channel."
        )


def _service_sid() -> str:
    sid = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "")
    if not sid:
        raise VerifyServiceNotConfigured(
            "TWILIO_VERIFY_SERVICE_SID not set — the Fazal-provisioned Twilio "
            "Verify Service SID is required for real Verify calls (D2)."
        )
    return sid


@lru_cache(maxsize=1)
def _client() -> Any:
    """Build the Twilio REST client from env.

    Lazy (not import-time) so importing this module needs no Twilio creds.
    When ``TEAM_TWILIO_VERIFY_MOCK_MODE=1`` the real client is never built —
    callers branch into the mock path before reaching here.
    """
    from twilio.rest import Client

    return Client(
        os.environ["TEAM_TWILIO_ACCOUNT_SID"],
        os.environ["TEAM_TWILIO_AUTH_TOKEN"],
    )


def start_verification(
    phone: str,
    channel: str = LIVE_CHANNEL,
    *,
    tenant_id: str | None = None,
) -> VerifyStartResult:
    """Start a Twilio Verify verification (deliver an OTP to ``phone``).

    ``phone`` MUST be E.164. ``channel`` ∈ {whatsapp (live), sms (gated)}.
    Returns a PII-safe ``VerifyStartResult`` (verification_sid + status).

    CL-390: the phone is NEVER logged; only verification_sid + tenant_id +
    channel + status reach a log line.
    """
    _validate_channel(channel)

    if _mock_mode():
        sid = f"VEmock{uuid4().hex[:26]}"
        logger.warning(
            "[TEAM_TWILIO_VERIFY_MOCK_MODE] start-verification: "
            "verification_sid=%s tenant_id=%s channel=%s status=pending",
            sid,
            tenant_id,
            channel,
        )
        return VerifyStartResult(verification_sid=sid, status="pending", channel=channel)

    service_sid = _service_sid()
    verification = (
        _client()
        .verify.v2.services(service_sid)
        .verifications.create(to=phone, channel=channel)
    )
    logger.info(
        "twilio-verify start: verification_sid=%s tenant_id=%s channel=%s status=%s",
        verification.sid,
        tenant_id,
        channel,
        verification.status,
    )
    return VerifyStartResult(
        verification_sid=verification.sid,
        status=verification.status,
        channel=channel,
    )


def check_verification(
    phone: str,
    code: str,
    *,
    tenant_id: str | None = None,
) -> VerifyCheckResult:
    """Check an OTP ``code`` against the verification started for ``phone``.

    ``phone`` MUST be E.164. Returns a PII-safe ``VerifyCheckResult``
    (approved bool + status + verification_sid). NEVER returns/logs the code.

    CL-390: neither phone nor code reaches a log line, an emitted exception
    message, or a returned field — only verification_sid + tenant_id + status.
    """
    if _mock_mode():
        approved = code == _mock_otp()
        status = "approved" if approved else "denied"
        sid = f"VEmock{uuid4().hex[:26]}"
        logger.warning(
            "[TEAM_TWILIO_VERIFY_MOCK_MODE] check-verification: "
            "verification_sid=%s tenant_id=%s status=%s",
            sid,
            tenant_id,
            status,
        )
        return VerifyCheckResult(verification_sid=sid, status=status, approved=approved)

    service_sid = _service_sid()
    check = (
        _client()
        .verify.v2.services(service_sid)
        .verification_checks.create(to=phone, code=code)
    )
    approved = check.status == "approved"
    logger.info(
        "twilio-verify check: verification_sid=%s tenant_id=%s status=%s approved=%s",
        getattr(check, "sid", None),
        tenant_id,
        check.status,
        approved,
    )
    return VerifyCheckResult(
        verification_sid=getattr(check, "sid", None),
        status=check.status,
        approved=approved,
    )
