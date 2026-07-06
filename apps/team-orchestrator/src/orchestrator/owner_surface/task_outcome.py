"""VT-611 pre-work #1 — the Team-Manager loop's owner-notification composer.

Closes the "truthful owner outcome" gap: ``manager/workflow.py``'s ``_settle_verified_task`` and
``_settle_declined_approval`` (mig 165, VT-605/606) have recorded ``terminal_outcome`` +
``owner_notification_status='pending'`` since the completion-verification checkpoint landed, but
NOTHING ever sent the owner anything — a completed/cancelled task settled silently. This module is
that send, scoped to the three outcomes the loop actually writes 'pending' for today:
``completed_with_effect`` / ``completed_no_action`` / ``cancelled`` (``failed``/``escalated`` land
the task at the NON-terminal 'blocked' status with a VTR incident — that is the operator's surface,
never 'pending', out of scope here).

Pattern: mirrors ``owner_surface/campaign_outcome.py::maybe_report_campaign_outcome`` byte-for-byte
in shape — a DETERMINISTIC bilingual (en/hi) composer (Pillar 1: no LLM, same inputs -> same text),
PII-safe (the objective text it quotes is already redacted at write, ``task_store.create_task`` ->
``pii_redactor.redact``), fail-soft throughout (a notification-send failure must NEVER unwind the
settle that already landed — logged + alerted, never raised).

Honesty (Pillar 7) is the whole point: ``cancelled`` MUST read as a DECLINE, never a success;
``completed_no_action`` MUST NOT claim an effect that didn't happen; ``completed_with_effect``
states plainly that the ask was carried out (using ``resolve_terminal_outcome``'s own
evidence-presence proxy — the loop does not fabricate specifics beyond what it verified).

The freeform-vs-template fork (24h WhatsApp customer-service window)
----------------------------------------------------------------------
These settles are usually triggered BY an owner turn (an approval resolve, an owner reply resuming
a paused step) — so the common case is INSIDE the window, and ``send_freeform_message`` (VT-44) is
Meta-compliant with no pre-approved template needed (same basis as VT-562/VT-349). OUTSIDE the
window a freeform send 63016s; the ONLY approved system-invoked re-engagement template today is
``team_reengage`` (VT-486, ``manager/stale_resume.py``) — but it carries a fixed, outcome-FREE
UTILITY body (it re-opens the window; it does not and cannot say "your task was cancelled"). Firing
it here and marking the notification 'delivered' would be dishonest — the owner would not actually
have been told the outcome. So outside the window this module does NOT send anything and does NOT
fabricate a content SID: it leaves ``owner_notification_status='pending'`` (deferred) and logs it.
A later owner-initiated turn re-opens the window; delivering the deferred outcome on that turn is
explicitly OUT of this row's scope (VT-611 pre-work builds the composer + the in-window send; a
re-trigger sweep is future work, not invented here).

Auditability: a successful send is recorded in the ``owner_notifications`` delivery ledger (VT-524)
under the label ``task_outcome_report``, keyed by the outbound message_sid — same ledger, same
async-callback delivery-tracking every other owner notification gets. ``manager_tasks.
owner_notification_status`` is a SEPARATE, narrower flag (mig 165's own comment: "the manager_task's
OWN view", not a duplicate store) — flips synchronously to 'delivered' the moment the send is
accepted by Twilio (this module's own idempotency key: a re-run only ever finds 'pending' once).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The owner_notifications ledger label for a free-form task-outcome report (NOT a Meta template
# name — the send is free-form; this identifies the notification class in the ledger).
_LEDGER_LABEL = "task_outcome_report"

# 24h-window-closed Twilio error (mirrors owner_surface.freeform_acks._WINDOW_CLOSED_CODE).
_WINDOW_CLOSED_CODE = 63016

# The three terminal_outcome values the loop's own settle steps write 'pending' for today
# (workflow.py's _settle_verified_task / _settle_declined_approval). 'failed'/'escalated' land the
# task at the non-terminal 'blocked' status with a VTR incident instead — never 'pending', so this
# module never sees them; listed here only as the explicit scope fence, checked defensively.
_HANDLED_OUTCOMES = frozenset({"completed_with_effect", "completed_no_action", "cancelled"})


def _extract_objective_text(task: dict[str, Any]) -> str:
    """The redacted, plain-text ask (``task_store.create_task``'s ``objective`` JSONB is always
    ``{"objective": <text>, ...}`` — mirrors ``verification.verify_completion``'s own extraction).
    Best-effort: any unexpected shape yields an empty string, never a crash."""
    doc = task.get("objective")
    if isinstance(doc, dict):
        return str(doc.get("objective") or "").strip()
    return ""


def compose_task_outcome_message(
    outcome: str, objective_text: str, *, locale: str = "en"
) -> str:
    """Compose the honest, owner-readable terminal-outcome message. Pure + deterministic.

    ``outcome`` must be one of ``_HANDLED_OUTCOMES``. ``objective_text`` may be empty (a redacted
    objective can legitimately have nothing left after PII stripping, or the field was never set) —
    the copy degrades to a generic phrasing rather than saying "None" or leaving a blank. ``locale``
    is 'en' or 'hi' (anything else falls back to 'en')."""
    obj = objective_text.strip()
    hi = locale == "hi"

    if outcome == "cancelled":
        # MUST read as a decline — never a success (the row's whole honesty gate).
        if hi:
            return (
                f"आपके कहने पर, मैंने इसे आगे नहीं बढ़ाया — अस्वीकृत: {obj}।" if obj else
                "आपके कहने पर, मैंने इसे आगे नहीं बढ़ाया — अस्वीकृत।"
            )
        return (
            f"As you asked, I did not go ahead with this — declined: {obj}." if obj else
            "As you asked, I did not go ahead with this — declined."
        )

    if outcome == "completed_no_action":
        # MUST NOT claim an effect that didn't happen.
        if hi:
            return (
                f"मैंने इसे देखा — {obj} — और कोई कार्रवाई की ज़रूरत नहीं थी।" if obj else
                "मैंने आपकी request देखी और कोई कार्रवाई की ज़रूरत नहीं थी।"
            )
        return (
            f"I looked into it — {obj} — and found no action was needed." if obj else
            "I looked into your request and found no action was needed."
        )

    # completed_with_effect: states plainly that the ask was carried out.
    if hi:
        return (
            f"हो गया — मैंने इसे पूरा कर दिया: {obj}।" if obj else
            "हो गया — आपने जो कहा था, मैंने पूरा कर दिया।"
        )
    return (
        f"Done — I've taken care of it: {obj}." if obj else
        "Done — I've taken care of what you asked."
    )


def _resolve_owner_phone(tenant_id: UUID | str) -> str | None:
    """Owner recipient: ``tenants.owner_phone`` falling back to ``whatsapp_number``. Mirrors
    ``campaign_outcome._resolve_owner_phone`` / ``request_owner_approval._resolve_owner_phone``
    verbatim. Best-effort: any error returns None (the notification defers, never crashes)."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:
        logger.exception("VT-611 task-outcome: owner-phone resolve failed tenant=%s", tenant_id)
        return None
    if row is None:
        return None
    row = dict(row)
    phone = row.get("owner_phone") or row.get("whatsapp_number")
    return str(phone) if phone else None


def maybe_notify_owner_of_task_outcome(
    tenant_id: UUID | str,
    task_id: UUID | str,
    *,
    recipient_phone: str | None = None,
) -> bool:
    """Send the owner the terminal-outcome notification for ``task_id``, if one is due.

    IDEMPOTENT: only sends when ``manager_tasks.owner_notification_status == 'pending'`` — that
    check IS the dedup, no separate message-id scheme needed (a re-run against an already-
    'delivered'/'failed' row is a clean no-op). Flips 'pending' -> 'delivered' on a successful
    dispatch, -> 'failed' on a definitive send error; leaves 'pending' UNCHANGED (deferred) when the
    24h window is closed (see module docstring) or the owner has no resolvable phone — both cases
    log a warning, neither raises. FAIL-SOFT throughout: this function never raises, because it runs
    immediately after the settle it is reporting on and must never unwind it (mirrors
    ``maybe_report_campaign_outcome``'s own binding fail-soft contract).

    Returns True only when a message was actually dispatched to Twilio.
    """
    from orchestrator.manager import task_store

    try:
        task = task_store.get_task(tenant_id, task_id)
    except Exception:
        logger.exception("VT-611 task-outcome: get_task failed tenant=%s task=%s", tenant_id, task_id)
        return False
    if task is None:
        logger.warning("VT-611 task-outcome: task not found tenant=%s task=%s", tenant_id, task_id)
        return False

    if task.get("owner_notification_status") != "pending":
        return False  # already handled (delivered/failed) or not required — the dedup

    outcome = task.get("terminal_outcome")
    if outcome not in _HANDLED_OUTCOMES:
        logger.warning(
            "VT-611 task-outcome: unhandled terminal_outcome=%r tenant=%s task=%s (skip, no flip)",
            outcome, tenant_id, task_id,
        )
        return False

    recipient = recipient_phone or _resolve_owner_phone(tenant_id)
    if not recipient:
        logger.warning(
            "VT-611 task-outcome: no owner phone tenant=%s task=%s — deferred (left pending)",
            tenant_id, task_id,
        )
        return False

    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

    locale = resolve_owner_locale(tenant_id)
    objective_text = _extract_objective_text(task)
    body = compose_task_outcome_message(outcome, objective_text, locale=locale)

    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        message_sid = send_freeform_message(
            body, recipient, tenant_id=tenant_id, surface="manager"
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: the settle already landed, never unwind it
        code = getattr(exc, "code", None)
        if code == _WINDOW_CLOSED_CODE:
            # Outside the 24h window: no fitting template exists for an outcome-bearing message
            # (team_reengage is content-free — see module docstring). Defer, don't fabricate.
            logger.info(
                "VT-611 task-outcome: 24h window closed tenant=%s task=%s — deferred (left pending)",
                tenant_id, task_id,
            )
            return False
        logger.exception(
            "VT-611 task-outcome: send failed tenant=%s task=%s code=%s", tenant_id, task_id, code,
        )
        task_store.set_owner_notification_status(
            tenant_id, task_id, "failed", expected_from=("pending",)
        )
        _alert_notify_send_failure(tenant_id, task_id)
        return False

    # Synchronous flip: the manager_task's OWN view of "has the owner been told" (mig 165 comment
    # — distinct from the VT-524 ledger's own accepted->delivered/failed async lifecycle below).
    task_store.set_owner_notification_status(
        tenant_id, task_id, "delivered", expected_from=("pending",)
    )

    from orchestrator.owner_surface.owner_notification import record_owner_notification

    record_owner_notification(tenant_id, _LEDGER_LABEL, message_sid, run_id=task_id)
    logger.info(
        "VT-611 task-outcome sent tenant=%s task=%s outcome=%s sid=%s",
        tenant_id, task_id, outcome, message_sid,
    )
    return True


def _alert_notify_send_failure(tenant_id: UUID | str, task_id: UUID | str) -> None:
    """Fire the ``outbound_failure`` critical alert for a definitive (non-window) send failure —
    the task settled but the owner's outcome confirmation did not go out. Fail-soft: an alert
    failure must never touch the caller."""
    try:
        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for

        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        dispatch_alert(Trigger(
            tenant_id=tid,
            trigger_kind="outbound_failure",
            severity=severity_for("outbound_failure"),
            run_id=UUID(str(task_id)),
            message_text=(
                "Task-outcome notification to the owner FAILED to send. The task reached a "
                "terminal state but the owner did not get the outcome — the owner is un-notified."
            ),
            payload={"surface": "task_outcome_report"},
        ))
    except Exception as exc:  # noqa: BLE001 — fail-soft: an alert failure must not affect the caller
        logger.warning("VT-611 task-outcome failure-alert dispatch failed (fail-soft): %s", exc)


__all__ = [
    "compose_task_outcome_message",
    "maybe_notify_owner_of_task_outcome",
]
