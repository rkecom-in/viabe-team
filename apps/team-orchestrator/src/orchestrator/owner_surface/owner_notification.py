"""VT-524 (B1) — owner-notification delivery ledger.

Closes the VT-519 delivery-blindness: a Twilio transport SID at send time proves
ACCEPTANCE, not delivery. This module records one ``owner_notifications`` row per
owner-facing template send (``accepted``), keyed by the outbound ``message_sid``,
then the async Twilio status callback flips it to ``delivered`` / ``failed``.

Both writers go through the RLS-bypassing service pool (``get_pool``) with an
explicit ``tenant_id`` — the orchestrator IS the service path (mirrors
``escalations`` / ``tm_audit_log``). Both are FAIL-SOFT: a ledger write must never
break the owner send or the inbound webhook (Pillar-1 the primary op wins;
observability is not a gate).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

# Twilio status-callback state → (owner_notification_status, communication_status).
# 'read' implies 'delivered'; 'undelivered' is a delivery failure like 'failed'.
_DELIVERED_STATES = frozenset({"delivered", "read"})
_FAILED_STATES = frozenset({"failed", "undelivered"})


def record_owner_notification(
    tenant_id: UUID | str,
    template_name: str,
    message_sid: str | None,
    *,
    run_id: UUID | str | None = None,
    status: str = "accepted",
) -> None:
    """Record an owner-notification send. ``status='accepted'`` (a transport SID proves
    acceptance, not delivery). No-op + logged on any error — never raises into the send."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO owner_notifications "
                "(tenant_id, run_id, template_name, message_sid, "
                " owner_notification_status, accepted_at) "
                "VALUES (%s, %s, %s, %s, %s, "
                "        CASE WHEN %s = 'accepted' THEN now() ELSE NULL END)",
                (
                    str(tenant_id),
                    str(run_id) if run_id else None,
                    template_name,
                    message_sid,
                    status,
                    status,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — fail-soft ledger; the send already happened
        logger.warning("owner_notification record failed (fail-soft): %s", exc)


def record_owner_notification_delivery(
    tenant_id: UUID | str,
    message_sid: str | None,
    callback_state: str | None,
) -> None:
    """Update the owner-notification row (by message_sid) from an async status callback:
    delivered/read → delivered; failed/undelivered → failed + failed_incident_open. Only
    flips a non-terminal row (pending/accepted) — the first terminal callback wins, no
    regression. No-op + logged on any error — never raises into the webhook."""
    if not message_sid or not callback_state:
        return
    if callback_state in _DELIVERED_STATES:
        new_status, comm, ts_col = "delivered", "delivered", "delivered_at"
    elif callback_state in _FAILED_STATES:
        new_status, comm, ts_col = "failed", "failed_incident_open", "failed_at"
    else:
        return  # unknown state — leave the ledger untouched
    try:
        with get_pool().connection() as conn:
            _update_delivery(conn, tenant_id, message_sid, new_status, comm, ts_col)
    except Exception as exc:  # noqa: BLE001 — fail-soft; the callback routing is unaffected
        logger.warning("owner_notification delivery update failed (fail-soft): %s", exc)


def _update_delivery(
    conn: Any,
    tenant_id: UUID | str,
    message_sid: str,
    new_status: str,
    comm: str,
    ts_col: str,
) -> None:
    # ts_col is one of two fixed literals (delivered_at/failed_at) — not user input.
    conn.execute(
        f"UPDATE owner_notifications "  # noqa: S608 — ts_col from a fixed 2-value set
        f"SET owner_notification_status = %s, communication_status = %s, {ts_col} = now() "
        "WHERE tenant_id = %s AND message_sid = %s "
        "  AND owner_notification_status IN ('pending', 'accepted')",
        (new_status, comm, str(tenant_id), message_sid),
    )


__all__ = ["record_owner_notification", "record_owner_notification_delivery"]
