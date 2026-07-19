"""VT-79 — breach notification helpers (Phase-1 slice).

``notify_owner`` sends a breach notice to the affected tenant's owner via
WhatsApp. Phase-1 uses a free-form session send with INTERIM copy (final
copy is VT-272/counsel-vetted, same posture as VT-303 / the breach runbook
DRAFT). The message text is PII-scrubbed before send (reuse alerts.pii_scrub).

DEFERRED (recorded in the VT-79 row):
- ``notify_customer`` — needs the live customer-inbound path + Meta templates
  (WABA go-live).
- ``notify_dpdpa_authority`` — manual process (Fazal/counsel send per CERT-In);
  the helper that drafts the email body lands with the runbook's final text.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.alerts.pii_scrub import scrub_pii
from orchestrator.graph import get_pool
from orchestrator.utils.twilio_send import send_freeform_message

logger = logging.getLogger(__name__)

# TODO(VT-272): final counsel-vetted breach-notice copy. Interim DRAFT.
_OWNER_NOTICE = (
    "Important security notice from Viabe: we detected a {severity} issue that "
    "may affect your account. Our team is investigating and will follow up. "
    "Summary: {summary}"
)


def notify_owner(tenant_id: UUID | str, severity: str, summary: str) -> dict[str, Any]:
    """Send a breach notice to the tenant owner's WhatsApp (free-form, scrubbed).

    Returns ``{"sent": bool, "sid": str|None, "error": str|None}`` — honest
    outcome (Pillar 7), never crashes the caller.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT whatsapp_number FROM tenants WHERE id = %s", (str(tenant_id),)
        )
        row = cur.fetchone()
    recipient = None
    if row is not None:
        recipient = row["whatsapp_number"] if isinstance(row, dict) else row[0]

    if not recipient:
        return {"sent": False, "sid": None, "error": "no owner whatsapp_number"}

    body = scrub_pii(
        _OWNER_NOTICE.format(severity=severity, summary=summary)
    )
    try:
        # VT-611 Package H0 — thread tenant_id so this notice lands in the lifetime conversation_log
        # (was bare -> _record_owner_conversation_turn no-op'd).
        sid = send_freeform_message(body, recipient, tenant_id=tenant_id, surface="system")
        return {"sent": True, "sid": sid, "error": None}
    except Exception as exc:  # noqa: BLE001 — honest outcome, never crash
        logger.warning("VT-79 notify_owner send failed (tenant=%s)", tenant_id)
        return {"sent": False, "sid": None, "error": repr(exc)}


__all__ = ["notify_owner"]
