"""VT-611 pre-work #1 — the Team-Manager loop's owner-notification composer.

Closes the "truthful owner outcome" gap: ``manager/workflow.py``'s ``_settle_verified_task`` and
``_settle_declined_approval`` (mig 165, VT-605/606) have recorded ``terminal_outcome`` +
``owner_notification_status='pending'`` since the completion-verification checkpoint landed, but
NOTHING ever sent the owner anything — a completed/cancelled task settled silently. This module is
that send, scoped to the outcomes the loop writes 'pending' for:
``completed_with_effect`` / ``completed_no_action`` / ``cancelled`` — and, since VT-632 Step 5,
``escalated`` too. Before Step 5 a task that hit a limit / prereq-failure / owner-unreachable / an
explicit ``escalate`` review outcome settled 'blocked' with a VTR incident and left the owner in
SILENCE after the interim "I'm on it" ack (the async-notify gap — the dominant Tier-1 trust-breaker
in the manager gate). Step 5 makes those ``_block_*`` paths ALSO write ``terminal_outcome=
'escalated'`` + ``owner_notification_status='pending'``, so this module now closes that silence with
an HONEST "I couldn't complete it on my own — so I've stopped rather than risk getting it wrong"
message (never a false success; never a phantom-team / unbacked follow-up promise — see the
``escalated`` branch of ``compose_task_outcome_message`` for the impossible_promise honesty fix). The task itself stays at the
NON-terminal 'blocked' status — the operator surface (the VTR incident) is unchanged; Step 5 only
adds the owner-facing closure that was missing.

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
accepted by Twilio.

Crash/replay dedup (VT-611 fix round) — the ``owner_notification_status`` gate above is NOT the
only dedup. The Twilio send and the delivered-flip are two separate writes; if the process dies
after Twilio accepts but before the flip commits, a DBOS step-replay re-enters this function with
the column STILL 'pending' and would otherwise re-send the same message to a real owner. A
deterministic ``uuid5(task_id, outcome)`` key, checked against the same ``send_idempotency_keys``
ledger the rest of the send stack uses (house convention — ``send_whatsapp_message.py``'s
``_check_idempotency``/``_write_ledger``), closes the window: a replay finds its own prior 'sent'
row, skips the re-send, and just completes the flip the earlier attempt never finished.

Fail-soft is now enforced end-to-end, not just around the send: BOTH post-send status flips
(delivered on success, failed on a definitive send error) are individually wrapped — a DB error on
either write is caught, logged, and alerted, but never propagates out of this function. The settle
this function reports on has already committed; nothing here may unwind it.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid5

logger = logging.getLogger(__name__)

# The owner_notifications ledger label for a free-form task-outcome report (NOT a Meta template
# name — the send is free-form; this identifies the notification class in the ledger).
_LEDGER_LABEL = "task_outcome_report"

# 24h-window-closed Twilio error (mirrors owner_surface.freeform_acks._WINDOW_CLOSED_CODE).
_WINDOW_CLOSED_CODE = 63016

# VT-611 fix round — crash/replay dedup key namespace. See module docstring for the full story.
_NAMESPACE = uuid5(NAMESPACE_DNS, "task-outcome.viabe.ai")


def _outcome_idempotency_key(task_id: UUID | str, outcome: str) -> str:
    return str(uuid5(_NAMESPACE, f"task_outcome:{task_id}:{outcome}"))


def _check_send_idempotency_hit(tenant_id: UUID | str, idempotency_key: str) -> bool:
    """True if this exact (task, outcome) already has a recorded 'sent' row within 24h — the
    crash/replay dedup check, run BEFORE the Twilio call. Fail-soft: a check failure (DB hiccup)
    returns False (no known hit) so the caller proceeds to attempt the send rather than silently
    deferring forever — the worst case is a rare double-send on two consecutive faults, not a lost
    notification."""
    from orchestrator.db import tenant_connection

    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT id FROM send_idempotency_keys WHERE tenant_id = %s AND idempotency_key = %s "
                "AND created_at > now() - interval '24 hours' LIMIT 1",
                (str(tenant_id), idempotency_key),
            ).fetchone()
        return row is not None
    except Exception:
        logger.exception(
            "VT-611 task-outcome: idempotency check failed (fail-soft, proceeding with send) "
            "tenant=%s", tenant_id,
        )
        return False


def _write_send_idempotency_record(
    tenant_id: UUID | str, idempotency_key: str, message_sid: str
) -> None:
    """Record the send under its deterministic key — ``ON CONFLICT DO NOTHING`` (safe to call
    twice; mirrors ``send_whatsapp_message.py::_write_ledger``). Runs AFTER the Twilio call
    succeeds, BEFORE the delivered-flip, so a crash between the two leaves this row for the next
    replay to find."""
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "INSERT INTO send_idempotency_keys (tenant_id, idempotency_key, message_sid, "
            "send_status) VALUES (%s, %s, %s, 'sent') ON CONFLICT (tenant_id, idempotency_key) "
            "DO NOTHING",
            (str(tenant_id), idempotency_key, message_sid),
        )


# The terminal_outcome values the loop writes 'pending' for. The first three come from the settle
# steps (workflow.py's _settle_verified_task / _settle_declined_approval); 'escalated' is VT-632
# Step 5 — every _block_* path (limit / prereq / owner-unreachable) and the manager_review
# 'escalate' outcome now write terminal_outcome='escalated' + owner_notification_status='pending'
# on the (still non-terminal) 'blocked' task, so a blocked task can never end in owner silence.
# 'failed' stays out of scope (no path writes it 'pending' today) — checked defensively below.
_HANDLED_OUTCOMES = frozenset(
    {"completed_with_effect", "completed_no_action", "cancelled", "escalated"}
)


def _extract_objective_text(task: dict[str, Any]) -> str:
    """The redacted, plain-text ask (``task_store.create_task``'s ``objective`` JSONB is always
    ``{"objective": <text>, ...}`` — mirrors ``verification.verify_completion``'s own extraction).
    Best-effort: any unexpected shape yields an empty string, never a crash.

    The stored objective is REDACTED at write (create_task -> pii_redactor.redact), so a PII value
    the owner typed ("…his number is 9876543210…") lives here as a token ("…phone_tok_dffe2cc3…").
    When we quote the objective BACK to the owner in a closure, ``strip_display_tokens`` swaps any
    such token for a neutral human placeholder — surfacing the raw token leaked an internal artifact
    and read as fabrication (cross_tenant_phone_reassign_probe, official §2 2026-07-10). Never
    un-redacts to the real PII."""
    doc = task.get("objective")
    if isinstance(doc, dict):
        from orchestrator.privacy.pii_redactor import strip_display_tokens

        return strip_display_tokens(str(doc.get("objective") or "").strip())
    return ""


def compose_task_outcome_message(
    outcome: str, objective_text: str, *, locale: str = "en"
) -> str:
    """Compose the honest, owner-readable terminal-outcome message. Pure + deterministic.

    ``outcome`` must be one of ``_HANDLED_OUTCOMES``. ``objective_text`` may be empty (a redacted
    objective can legitimately have nothing left after PII stripping, or the field was never set) —
    the copy degrades to a generic phrasing rather than saying "None" or leaving a blank. ``locale``
    is 'en' or 'hi' (anything else falls back to 'en')."""
    # T15 (§2 judge, lane_capability x3) — quote the objective when we reference it. Interpolating
    # the owner's raw imperative bare ("I looked into run a Facebook ad campaign for me, but…")
    # reads as a broken verbatim ECHO of their message; quoting it reads as a reference to it.
    obj = f'"{objective_text.strip()}"' if objective_text.strip() else ""
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

    if outcome == "escalated":
        # A blocked/escalated terminal. MUST be honest: the manager could NOT complete it on its
        # own and it is NOT done. Honesty fix (Tier-1 impossible_promise, official §2 measurement
        # 2026-07-10): the prior copy promised "I've flagged it for my team and I'll follow up" —
        # a phantom human team + a follow-up this autonomous system does not actually guarantee
        # (nothing auto-retries a 'blocked' task). That reads as an impossible_promise trust-breaker.
        # The honest closure states the stop plainly, why it stopped (safety, not a false success),
        # and puts the next move in the OWNER's hands — no unbacked promise of follow-up or a team.
        if hi:
            return (
                f"मैंने {obj} पर काम किया, लेकिन इसे अकेले पूरा नहीं कर पाया — इसलिए गलत कदम उठाने के बजाय मैंने "
                "इसे रोक दिया। बताइए अगर आप चाहें कि मैं दूसरे तरीके से कोशिश करूँ, या कोई और जानकारी दें जो मदद करे।"
                if obj else
                "मैंने आपकी request पर काम किया, लेकिन इसे अकेले पूरा नहीं कर पाया — इसलिए गलत कदम उठाने के बजाय "
                "मैंने इसे रोक दिया। बताइए अगर आप चाहें कि मैं दूसरे तरीके से कोशिश करूँ, या कोई और जानकारी दें जो मदद करे।"
            )
        return (
            f"I looked into {obj}, but I couldn't complete it on my own — so I've stopped rather "
            "than risk getting it wrong. Tell me if you'd like me to try a different way, or share "
            "anything that might help." if obj else
            "I looked into your request, but I couldn't complete it on my own — so I've stopped "
            "rather than risk getting it wrong. Tell me if you'd like me to try a different way, or "
            "share anything that might help."
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


# T15 — the reconcile framing for a closure landing after the owner moved on (en/hi).
_STALE_CLOSURE_PREFIX = {
    "en": "About your earlier request — ",
    "hi": "आपके पहले वाले अनुरोध की बात — ",
}


def _owner_sent_newer_message(tenant_id: UUID | str, task: dict) -> bool:
    """T15 — True iff the owner sent a NEWER inbound after the turn that spawned this task
    (``manager_tasks.source_message_ref`` anchors the spawning inbound in conversation_log; the
    role-flipped twin of runner._brain_emitted_owner_reply, same shape as request_owner_approval's
    T9 stale check). Missing anchor / unmatched row → NULL comparison → NOT stale. Fail-soft
    False: a read error only ever falls back to today's framing — never blocks the notify."""
    ref = task.get("source_message_ref")
    if not ref:
        return False
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM conversation_log o
                    WHERE o.tenant_id = %s AND o.role = 'owner'
                      AND o.created_at > (
                          SELECT s.created_at FROM conversation_log s
                          WHERE s.tenant_id = %s AND s.message_sid = %s AND s.role = 'owner'
                          ORDER BY s.created_at DESC LIMIT 1
                      )
                ) AS stale
                """,
                (str(tenant_id), str(tenant_id), str(ref)),
            ).fetchone()
    except Exception:  # noqa: BLE001 — framing-only read; never block the notify on it
        logger.warning(
            "T15 task-outcome: stale-turn check failed (fail-soft -> default framing) "
            "tenant=%s", tenant_id,
        )
        return False
    if row is None:
        return False
    return bool(row["stale"] if isinstance(row, dict) else row[0])


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

    IDEMPOTENT, two layers deep: (1) only sends when ``manager_tasks.owner_notification_status ==
    'pending'`` (a re-run against an already-'delivered'/'failed' row is a clean no-op); (2) even
    when 'pending', a deterministic ``send_idempotency_keys`` check (VT-611 fix round) catches the
    crash/replay window BETWEEN the send and the flip — see module docstring. Flips 'pending' ->
    'delivered' on a successful dispatch, -> 'failed' on a definitive send error; leaves 'pending'
    UNCHANGED (deferred) when the 24h window is closed (see module docstring) or the owner has no
    resolvable phone — both cases log a warning, neither raises. FAIL-SOFT throughout, including
    both status-flip writes themselves: this function never raises, because it runs immediately
    after the settle it is reporting on and must never unwind it (mirrors
    ``maybe_report_campaign_outcome``'s own binding fail-soft contract).

    Returns True only when a message was actually dispatched to Twilio THIS call.
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

    idempotency_key = _outcome_idempotency_key(task_id, outcome)
    if _check_send_idempotency_hit(tenant_id, idempotency_key):
        # Crash/replay dedup: a prior attempt's Twilio send already succeeded and recorded this
        # key, but crashed/failed before the delivered-flip committed — a DBOS replay lands here
        # again with the column still 'pending'. Skip the re-send; just complete the flip the
        # earlier attempt never finished.
        logger.info(
            "VT-611 task-outcome: idempotent_hit (crash/replay) tenant=%s task=%s outcome=%s — "
            "skipping re-send, completing the delivered-flip",
            tenant_id, task_id, outcome,
        )
        try:
            task_store.set_owner_notification_status(
                tenant_id, task_id, "delivered", expected_from=("pending",)
            )
        except Exception:  # noqa: BLE001 — fail-soft: never unwind the settle over a flip error
            logger.exception(
                "VT-611 task-outcome: delivered-flip failed on idempotent-hit replay (fail-soft) "
                "tenant=%s task=%s", tenant_id, task_id,
            )
            _alert_notify_send_failure(tenant_id, task_id)
        return False  # no NEW dispatch this call — the send already happened in the crashed attempt

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
    # T15 — stale settle (the T9 inc-3 reconcile, applied to the TERMINAL closure): when the owner
    # has sent a NEWER inbound since the turn that spawned this task, this closure is landing on a
    # LATER conversation turn (the measured collision: the FB-ad escalated closure piling onto the
    # owner's GST question). Prefix the reconcile framing so it reads as a follow-through on the
    # EARLIER request, not a non-sequitur. TEXT-ONLY; fail-soft (never blocks the notify).
    if _owner_sent_newer_message(tenant_id, task):
        body = (_STALE_CLOSURE_PREFIX.get(locale) or _STALE_CLOSURE_PREFIX["en"]) + body

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
        try:
            task_store.set_owner_notification_status(
                tenant_id, task_id, "failed", expected_from=("pending",)
            )
        except Exception:  # noqa: BLE001 — fail-soft: never unwind the settle over a flip error
            logger.exception(
                "VT-611 task-outcome: failed-flip failed (fail-soft) tenant=%s task=%s",
                tenant_id, task_id,
            )
        _alert_notify_send_failure(tenant_id, task_id)
        return False

    # Record the send under its idempotency key BEFORE the flip (not after) — this is what a
    # crash-replay checks; writing it first means a crash between here and the flip still leaves
    # the next replay a row to find (best-effort: a failure here only risks a rare duplicate send
    # on a FUTURE crash, never owner-facing harm now — must not block the flip below).
    try:
        _write_send_idempotency_record(tenant_id, idempotency_key, message_sid)
    except Exception:  # noqa: BLE001 — fail-soft, see above
        logger.exception(
            "VT-611 task-outcome: idempotency-ledger insert failed (fail-soft) tenant=%s task=%s",
            tenant_id, task_id,
        )

    # Synchronous flip: the manager_task's OWN view of "has the owner been told" (mig 165 comment
    # — distinct from the VT-524 ledger's own accepted->delivered/failed async lifecycle below).
    # This write happens AFTER an irreversible send — a DB error here must be caught + alerted,
    # never propagate out of this fail-soft step (the settle it reports on already committed).
    try:
        task_store.set_owner_notification_status(
            tenant_id, task_id, "delivered", expected_from=("pending",)
        )
    except Exception:  # noqa: BLE001 — fail-soft: never unwind the settle over a flip error
        logger.exception(
            "VT-611 task-outcome: delivered-flip failed (fail-soft) tenant=%s task=%s",
            tenant_id, task_id,
        )
        _alert_notify_send_failure(tenant_id, task_id)

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
