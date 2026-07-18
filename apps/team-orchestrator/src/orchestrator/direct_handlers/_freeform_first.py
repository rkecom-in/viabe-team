"""VT-683 P1 — freeform-first delivery for REACTIVE direct handlers.

These handlers fire in direct response to an owner INBOUND (STOP / status ping / DSR keyword),
so the 24h session window is open by construction — a Meta template is unnecessary for the
reply (Fazal whitelist ruling 2026-07-18: owner template surface = OTP/welcome/wake-up only).

Transition belt: the old template is kept as a FALLBACK when the freeform send fails (e.g. a
redelivered webhook processed hours later, past the window — Twilio 63016). Compliance surfaces
(opt-out confirmation, DSR ack) MUST reach the owner, so the belt stays until the P4 whitelist
review retires it on delivery evidence. The returned dict always reports the real outcome
(Pillar 7): which channel actually carried the message, or that both failed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def send_freeform_first(
    tenant_id: Any,
    body: str,
    recipient_phone: str | None,
    *,
    fallback_template: str,
    fallback_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send ``body`` as an in-session freeform message; on ANY freeform failure fall back to the
    Meta template. Returns a truthful send_result dict:

    ``{"success": bool, "channel": "freeform_session"|"template_fallback"|"none",
       "message_sid": str|None, "template_name": <fallback name>, "error": str|None}``

    ``template_name`` is always present (test/audit continuity with the pre-P1 contract —
    it names the template that WOULD have been used / was used on fallback).
    """
    from orchestrator.utils.twilio_send import send_freeform_message, send_template_message

    if recipient_phone:
        try:
            sid = send_freeform_message(
                body, recipient_phone, tenant_id=tenant_id, surface="system",
            )
            return {
                "success": True, "channel": "freeform_session", "message_sid": sid,
                "template_name": fallback_template, "error": None,
            }
        except Exception as exc:  # noqa: BLE001 — fall back to the template belt
            logger.warning(
                "freeform-first: in-session send failed (%s) — template fallback %r tenant=%s",
                type(exc).__name__, fallback_template, tenant_id,
            )

    result = send_template_message(
        tenant_id, fallback_template, fallback_params or {}, recipient_phone=recipient_phone,
    )
    dumped = result.model_dump()  # SendResult: success/message_sid/error_code/error_message/
    dumped["channel"] = "template_fallback"  # attempted_at/template_name/recipient_phone_token
    return dumped


__all__ = ["send_freeform_first"]
