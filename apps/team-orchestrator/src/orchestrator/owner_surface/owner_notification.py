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
            rowcount = _update_delivery(conn, tenant_id, message_sid, new_status, comm, ts_col)
    except Exception as exc:  # noqa: BLE001 — fail-soft; the callback routing is unaffected
        logger.warning("owner_notification delivery update failed (fail-soft): %s", exc)
        return
    # VT-534 (B1-part-2): on a NEW failed transition (a real row flipped, not a terminal-safe
    # no-op re-callback), fire the outbound_failure alert — the declared-but-never-fired critical
    # detector. This is the reviewer-visibility stage: an owner was ACCEPTED but delivery failed,
    # so the owner is silently un-notified (the exact VT-519 class). Separate fail-soft guard: an
    # alert failure must never touch the ledger write or the inbound webhook.
    if new_status == "failed" and rowcount > 0:
        _alert_owner_delivery_failure(tenant_id, message_sid)


def _alert_owner_delivery_failure(tenant_id: UUID | str, message_sid: str) -> None:
    """Fire the outbound_failure critical alert for a failed owner-notification delivery.

    Dev-routed by ``dispatch_alert`` (a dev/canary tenant reaches only the dev bot, never pages
    Fazal), and fully fail-soft. The message_sid is an opaque Twilio id, not PII; ``dispatch_alert``
    additionally PII-scrubs the text."""
    try:
        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for

        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        dispatch_alert(Trigger(
            tenant_id=tid,
            trigger_kind="outbound_failure",
            severity=severity_for("outbound_failure"),
            message_text=(
                f"Owner-notification delivery FAILED (message_sid={message_sid}). The owner was "
                "accepted by Twilio but Meta/carrier did not deliver — the owner is un-notified. "
                "Investigate template category / opt-in / number."
            ),
            payload={"surface": "owner_notification", "message_sid": message_sid},
        ))
    except Exception as exc:  # noqa: BLE001 — fail-soft: an alert failure must not affect the ledger/webhook
        logger.warning("owner_notification failure-alert dispatch failed (fail-soft): %s", exc)


def _update_delivery(
    conn: Any,
    tenant_id: UUID | str,
    message_sid: str,
    new_status: str,
    comm: str,
    ts_col: str,
) -> int:
    """Apply the terminal-safe delivery flip; return the affected row count (0 = a no-op
    re-callback on an already-terminal row, so the caller does not re-fire the alert)."""
    # ts_col is one of two fixed literals (delivered_at/failed_at) — not user input.
    cur = conn.execute(
        f"UPDATE owner_notifications "  # noqa: S608 — ts_col from a fixed 2-value set
        f"SET owner_notification_status = %s, communication_status = %s, {ts_col} = now() "
        "WHERE tenant_id = %s AND message_sid = %s "
        "  AND owner_notification_status IN ('pending', 'accepted')",
        (new_status, comm, str(tenant_id), message_sid),
    )
    return cur.rowcount


__all__ = ["record_owner_notification", "record_owner_notification_delivery"]
