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

        # VT-683 P2c — an 'approval' item whose underlying pending_approvals row is no longer OPEN
        # (resolved by reply, timeout sweep, or rollback) must NEVER be delivered: the owner would
        # tap a button against a row that cannot resolve (the exact VT-615 dropped-campaign shape).
        # Drop it honestly and let the next drain pick the next item.
        if item.get("kind") == "approval":
            ref = item.get("decision_ref") or {}
            ref_id = ref.get("id") if isinstance(ref, dict) else None
            still_open = None
            if ref_id:
                from orchestrator.db.wrappers import PendingApprovalsWrapper

                still_open = PendingApprovalsWrapper().get_open_by_id(tenant_id, ref_id)
            if not still_open:
                q.drop_item(tenant_id, item["id"], reason="resolved_elsewhere")
                logger.info(
                    "owner_comms drain: approval item %s dropped (underlying ask no longer open)",
                    item["id"],
                )
                return None
            # POINT A duplicate guard: a RUNNING decision clock (timeout_at non-NULL) means the
            # ask already reached the owner (the arm delivered it but its fail-soft
            # mark_delivered didn't land) — re-sending would be a duplicate ask. Drop the
            # ledger straggler; the open row stays fully resolvable.
            if still_open.get("timeout_at") is not None:
                q.drop_item(tenant_id, item["id"], reason="already_delivered")
                logger.info(
                    "owner_comms drain: approval item %s dropped (ask already delivered — "
                    "clock running)", item["id"],
                )
                return None
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

        # VT-693 — an item may request INTERACTIVE delivery (payload.interactive_template = a
        # registry name for an in-session quick-reply Content object; the body rides {{1}}).
        # Falls back to the plain freeform body on ANY interactive failure — delivery is never
        # lost to presentation. (The GST identity card uses onboarding_confirm_yesno.)
        sid = None
        delivered_interactive = False
        interactive_name = str(payload.get("interactive_template") or "")
        if interactive_name and recipient_phone:
            try:
                from orchestrator.templates_registry import content_sid_for
                from orchestrator.utils.twilio_send import send_interactive_message

                content_sid = content_sid_for(interactive_name, "en")
                if content_sid:
                    # VT-695 — a multi-variable object (the formatted GST card) carries its own
                    # per-field values; the default stays body-as-{{1}} (single-var objects).
                    _vars = payload.get("interactive_variables") or {"1": body}
                    sid = send_interactive_message(
                        content_sid,
                        recipient_phone,
                        content_variables={str(k): str(v) for k, v in _vars.items()},
                        tenant_id=tenant_id,
                        surface="manager",
                    )
                    delivered_interactive = True
            except Exception:  # noqa: BLE001 — presentation fallback, never a lost delivery
                logger.warning(
                    "owner_comms drain: interactive send failed — freeform fallback item=%s",
                    item["id"],
                )
        if not delivered_interactive:
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
        # VT-683 P2c — a deferred task-outcome item (task_outcome's 63016 window-closed path
        # enqueued it) flips the manager_task's own "owner told" status on delivery, so the
        # settle ledger stays truthful. Fail-soft: the delivery already happened.
        task_ref = payload.get("manager_task_id")
        if task_ref:
            try:
                from orchestrator.owner_surface.task_outcome import (
                    mark_deferred_outcome_delivered,
                )

                mark_deferred_outcome_delivered(tenant_id, task_ref, sid)
            except Exception:  # noqa: BLE001 — ledger flip only; never unwind the delivery
                logger.warning(
                    "owner_comms drain: deferred-outcome flip failed (fail-soft) task=%s", task_ref
                )
        if delivered_interactive:
            channel = "interactive_session"
        else:
            channel = res.get("channel") if isinstance(res, dict) else None
        return {
            "delivered": True,
            "item_id": item["id"],
            "kind": item["kind"],
            "message_sid": sid,
            "channel": channel,
        }
    except Exception:  # noqa: BLE001 — best-effort; a drain must never break the caller
        logger.warning("owner_comms drain_one failed (best-effort)", exc_info=True)
        return None


__all__ = ["SWEEP_IDLE_MINUTES", "drain_one"]
