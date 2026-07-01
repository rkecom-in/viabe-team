"""VT-393 — the owner-utility template-send seam.

A thin, lang-aware wrapper over ``utils.twilio_send.send_template_message`` — the
SINGLE seam for sending an owner-facing WhatsApp template (welcome, trial-expiry,
etc.). It is the PRIMITIVE: callers adapt their own param shapes to it.

Pillar 7: it returns the unmodified ``SendResult`` — no hardcoded success, no
swallowed failure. The honesty invariant lives in the underlying send.
Pillar 3: the recipient phone is tokenised in the SendResult by the underlying
          send; this wrapper never logs or returns it in plaintext.

It is built reusable (e.g. for trial_sweep's owner notify) but only the welcome
seam is wired in this row; other callers wire separately.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from orchestrator.utils.twilio_send import SendResult, send_template_message


def send_owner_template(
    tenant_id: UUID,
    template_name: str,
    language: str,
    params: dict[str, Any],
    *,
    recipient_phone: str,
) -> SendResult:
    """Send an owner-facing template in the owner's language.

    Args:
        tenant_id: the owning tenant.
        template_name: a registry template name (e.g. ``team_welcome``).
        language: the language variant to resolve the SID for (e.g. ``en``/``hi``).
        params: the template content variables (caller-shaped).
        recipient_phone: the owner's WhatsApp number (E.164). REQUIRED here — the
            owner surface always targets an explicit owner number, never the
            tenant's default whatsapp_number fallback.

    Returns the underlying ``SendResult`` unchanged (success only on a confirmed
    send; an unapproved SID → success=False / ``template_not_yet_approved``).
    Raises whatever ``send_template_message`` raises (UnknownTemplateError for an
    unknown template; a 5xx re-raise for DBOS retry).
    """
    result = send_template_message(
        tenant_id,
        template_name,
        params,
        recipient_phone=recipient_phone,
        language=language,
    )
    # VT-524 (B1): record this owner notification in the delivery ledger — 'accepted' (a transport
    # SID proves acceptance, not delivery). The async Twilio status callback later flips it to
    # delivered/failed (runner). Fail-soft: never perturb the send result.
    if result.success and result.message_sid:
        from orchestrator.owner_surface.owner_notification import (
            record_owner_notification,
        )

        record_owner_notification(tenant_id, template_name, result.message_sid)
    return result


__all__ = ["send_owner_template"]
