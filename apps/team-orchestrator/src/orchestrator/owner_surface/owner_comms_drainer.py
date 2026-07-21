"""VT-683 P2b — the owner-comms queue drainer (idle-pace delivery inside an open session).

Delivers queued owner-comms ONE item at a time, ONLY when a 24h session is open, at idle pace
(never mid-exchange — enforced by WHERE this is called: the post-turn hook fires after a completed
owner turn; the scheduled sweep gates on ``idle_minutes >= threshold``). Each delivery goes out as
an in-session FREEFORM message (no Meta template) and then marks the item delivered — which, for an
approval, starts its decision clock (POINT A, ``owner_comms_queue.mark_delivered``).

The queued ``payload`` is PRERENDERED by the writer (P2c): ``{"text_en"/"text_hi": <body>,
"fallback_template": <name>}``. The drainer is render-agnostic — it just sends the right-locale body.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The scheduled sweep only delivers to a tenant idle at least this long — so a queued item never
# lands mid-exchange (the owner is mid-typing). The post-turn hook has no such gate (the turn just
# COMPLETED, so it is an idle moment by construction).
SWEEP_IDLE_MINUTES = 5.0


def drain_one(
    tenant_id: UUID | str,
    recipient_phone: str | None,
    *,
    lang: str = "en",
) -> dict[str, Any] | None:
    """Deliver ONE highest-priority queued item iff a session is open. Returns a delivery summary
    or None (session closed / queue empty / empty body). Best-effort — NEVER raises into the caller
    (this runs on the post-turn hot path + the sweep; a drain failure must never break either).
    """
    try:
        from orchestrator.owner_surface.session_window import session_open

        if not session_open(tenant_id):
            return None

        from orchestrator.owner_surface import owner_comms_queue as q

        item = q.next_deliverable(tenant_id)
        if item is None:
            return None

        payload = item.get("payload") or {}
        body = (
            payload.get(f"text_{lang}")
            or payload.get("text_en")
            or payload.get("text")
            or ""
        )
        if not body:
            # A payload with no renderable body is a writer bug — drop it (honest-expiry), don't
            # deliver an empty message. Mark delivered so it doesn't wedge the queue head forever.
            q.mark_delivered(tenant_id, item["id"], kind=item["kind"], message_sid=None)
            logger.warning("owner_comms drain: item %s had no body — marked delivered empty", item["id"])
            return None

        from orchestrator.direct_handlers._freeform_first import send_freeform_first

        res = send_freeform_first(
            tenant_id,
            body,
            recipient_phone,
            fallback_template=str(payload.get("fallback_template") or "team_error_handler"),
        )
        sid = res.get("message_sid") if isinstance(res, dict) else None
        # POINT A: mark_delivered starts an approval's decision clock (delivered_at + TTL).
        q.mark_delivered(tenant_id, item["id"], kind=item["kind"], message_sid=sid)
        return {
            "delivered": True,
            "item_id": item["id"],
            "kind": item["kind"],
            "message_sid": sid,
            "channel": res.get("channel") if isinstance(res, dict) else None,
        }
    except Exception:  # noqa: BLE001 — best-effort; a drain must never break the caller
        logger.warning("owner_comms drain_one failed (best-effort)", exc_info=True)
        return None


__all__ = ["SWEEP_IDLE_MINUTES", "drain_one"]
