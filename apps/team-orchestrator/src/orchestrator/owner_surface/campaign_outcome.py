"""VT-562 — per-run campaign outcome report to the owner.

Closes the "ends at executed" gap: when an owner approves a campaign, the resume
path (runner.try_resume_pending_approval) runs the ``campaign_execute`` node,
which returns ``campaign_execution_summary`` {sent, skipped_opt_out,
skipped_complaint_freeze, failed, killed} — and NOTHING consumed it. After real
WhatsApp sends the owner heard nothing back. This module composes an honest,
owner-readable outcome from that summary and sends it to the owner.

Send seam (why free-form, not a template)
-----------------------------------------
The owner has JUST replied (the approval reply is the inbound that drives the
resume), so we are inside the 24h WhatsApp customer-service window → a free-form
session message is Meta-compliant with NO pre-approved template (Fazal ruling
2026-06-06, same basis as the VT-349 free-form acks). The counts vary per run
and reasons appear only when non-zero, which a fixed positional template cannot
express. So the report sends via ``utils.twilio_send.send_freeform_message`` —
the SAME guarded transport chokepoint every send funnels through (``_client()`` →
the VT-476 dev send-guard: a dev send to a non-allowlisted number is MOCKED).

Auditability: a successful send is recorded in the ``owner_notifications``
delivery ledger (VT-524) under the label ``campaign_outcome_report``, keyed by the
outbound message_sid. The async Twilio status callback (runner) then flips it to
delivered/failed and fires the ``outbound_failure`` alert on a failed delivery —
so the outcome report is delivery-tracked like every other owner notification.
(``owner_message_audit`` is the DSR reconstruction substrate written by the
customer-send path; the owner-facing delivery ledger is ``owner_notifications``.)

Honesty (Pillar 7): counts stated plainly; ``sent`` means DISPATCHED, never
"delivered" (delivery is a separate signal); skipped/failed/killed are surfaced
whenever non-zero, never hidden; a zero-sent run says so explicitly.

Pillar 1: deterministic — NO LLM. The composer is pure so it is unit-testable and
stays honest (same inputs → same text).
Pillar 3: the owner phone is never logged; only the tenant_id + counts appear.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The owner_notifications ledger label for a free-form campaign-outcome report (NOT a Meta
# template name — the send is free-form; this identifies the notification class in the ledger).
_LEDGER_LABEL = "campaign_outcome_report"

# The five count axes execute_approved_campaign reports (campaign/execute.py). A held/blocked/
# error terminal carries a DIFFERENT shape (status/pre_gate_blocked/no counts) — read defensively.
_COUNT_KEYS = ("sent", "skipped_opt_out", "skipped_complaint_freeze", "failed", "killed")


def _counts(summary: dict[str, Any]) -> dict[str, int]:
    """Coerce the five count axes off a summary dict, defaulting absent/non-int keys to 0."""
    out: dict[str, int] = {}
    for key in _COUNT_KEYS:
        try:
            out[key] = int(summary.get(key, 0) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def summary_has_activity(summary: dict[str, Any] | None) -> bool:
    """True iff the summary reflects a real fan-out worth reporting.

    Guards against the non-execution terminals that also reach the resume path: a missing
    summary (a rejected / needs_changes resume never ran ``campaign_execute``), a run-control
    HOLD ({"status": "held_by_run_control"} — no send happened), and an all-zero summary. Only a
    summary with at least one non-zero count axis triggers an owner report.
    """
    if not summary:
        return False
    return any(v > 0 for v in _counts(summary).values())


def _n(count: int, singular: str, plural: str) -> str:
    """English count + noun with singular/plural agreement ("1 customer" / "3 customers")."""
    return f"{count} {singular if count == 1 else plural}"


def compose_campaign_outcome_message(
    summary: dict[str, Any], *, locale: str = "en"
) -> str:
    """Compose the honest, owner-readable campaign-outcome message. Pure + deterministic.

    ``locale`` is 'en' or 'hi' (anything else falls back to 'en'). ``sent`` is phrased as
    DISPATCHED, never "delivered". Every non-zero skipped/failed/killed axis is stated; a
    zero-sent run is stated as such.
    """
    c = _counts(summary)
    sent = c["sent"]
    lines: list[str] = []

    if locale == "hi":
        if sent > 0:
            lines.append(f"आपका कैंपेन भेज दिया गया है — मैंने इसे {sent} ग्राहकों को भेजा।")
        else:
            lines.append("आपका कैंपेन पूरा हो गया, लेकिन मैं इसे किसी ग्राहक को नहीं भेज सका।")
        if c["skipped_opt_out"]:
            lines.append(f"{c['skipped_opt_out']} को छोड़ा गया क्योंकि उन्होंने मैसेज बंद कर रखे हैं।")
        if c["skipped_complaint_freeze"]:
            lines.append(f"{c['skipped_complaint_freeze']} को शिकायत-होल्ड के कारण छोड़ा गया।")
        if c["failed"]:
            lines.append(f"{c['failed']} मैसेज किसी त्रुटि के कारण नहीं भेजे जा सके।")
        if c["killed"]:
            lines.append(f"{c['killed']} से संपर्क नहीं हुआ क्योंकि कैंपेन रोक दिया गया था।")
        return " ".join(lines)

    # Default: English.
    if sent > 0:
        lines.append(f"Your campaign has gone out — I sent it to {_n(sent, 'customer', 'customers')}.")
    else:
        lines.append("Your campaign has finished, but I couldn't send it to any customers.")
    if c["skipped_opt_out"]:
        lines.append(
            f"{_n(c['skipped_opt_out'], 'customer was', 'customers were')} "
            "skipped because they've opted out of messages."
        )
    if c["skipped_complaint_freeze"]:
        lines.append(
            f"{_n(c['skipped_complaint_freeze'], 'customer was', 'customers were')} "
            "skipped because of a complaint hold."
        )
    if c["failed"]:
        lines.append(
            f"{_n(c['failed'], 'message', 'messages')} couldn't be sent due to an error."
        )
    if c["killed"]:
        lines.append(
            f"{_n(c['killed'], 'customer was', 'customers were')} "
            "not contacted because the campaign was stopped."
        )
    return " ".join(lines)


def _resolve_owner_phone(tenant_id: UUID | str) -> str | None:
    """Owner recipient for the report: ``tenants.owner_phone`` (the owner's personal anchor,
    migration 050), falling back to the tenant's ``whatsapp_number``. Tenant-scoped read (RLS via
    ``tenant_connection``); mirrors request_owner_approval._resolve_owner_phone. Best-effort: any
    error returns None (the report is skipped, never crashes the resume)."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:
        logger.exception("VT-562 owner-phone resolve failed tenant=%s", tenant_id)
        return None
    if row is None:
        return None
    row = dict(row)
    phone = row.get("owner_phone") or row.get("whatsapp_number")
    return str(phone) if phone else None


def maybe_report_campaign_outcome(
    tenant_id: UUID | str,
    terminal_state: Any,
    *,
    run_id: UUID | str | None = None,
    recipient_phone: str | None = None,
) -> bool:
    """Report the campaign-execution outcome to the owner, if the resume actually executed one.

    Reads ``campaign_execution_summary`` off the resume terminal state. No-summary /
    no-activity / held / blocked → no send (returns False) — a rejected/needs_changes resume,
    a run-control hold, and an all-zero run each correctly produce no report.

    FAIL-SOFT (binding): the campaign already sent, so a report-send failure must NEVER unwind
    the resume/close. Every failure is logged and swallowed (returns False); a send failure
    additionally fires the existing ``outbound_failure`` alert so an un-notified owner is
    surfaced. Returns True only when a report was dispatched to Twilio.
    """
    summary = None
    if isinstance(terminal_state, dict):
        summary = terminal_state.get("campaign_execution_summary")
    if not summary_has_activity(summary):
        logger.info(
            "VT-562 outcome-report: no reportable summary tenant=%s (skip)", tenant_id
        )
        return False

    recipient = recipient_phone or _resolve_owner_phone(tenant_id)
    if not recipient:
        logger.warning(
            "VT-562 outcome-report: no owner phone tenant=%s — owner un-notified (skip)",
            tenant_id,
        )
        return False

    from orchestrator.owner_surface.freeform_acks import resolve_owner_locale

    locale = resolve_owner_locale(tenant_id)
    body = compose_campaign_outcome_message(summary, locale=locale)

    try:
        from orchestrator.utils.twilio_send import send_freeform_message

        # VT-611 Package H0 — thread tenant_id so this owner-facing outcome report lands in the
        # lifetime conversation_log (was bare -> _record_owner_conversation_turn no-op'd, invisible
        # to the loop's own memory + any transcript-based assert/judge).
        message_sid = send_freeform_message(body, recipient, tenant_id=tenant_id, surface="manager")
    except Exception as exc:  # noqa: BLE001 — the campaign sent; the report must never crash the resume
        code = getattr(exc, "code", None)
        logger.exception(
            "VT-562 outcome-report send failed tenant=%s code=%s — owner un-notified",
            tenant_id, code,
        )
        _alert_report_send_failure(tenant_id, run_id)
        return False

    # Auditable: record in the owner_notifications delivery ledger (VT-524) — 'accepted' (a
    # transport SID proves acceptance, not delivery). The async status callback flips it to
    # delivered/failed + fires outbound_failure on a failed delivery. Fail-soft internally.
    from orchestrator.owner_surface.owner_notification import record_owner_notification

    record_owner_notification(tenant_id, _LEDGER_LABEL, message_sid, run_id=run_id)
    logger.info(
        "VT-562 outcome-report sent tenant=%s run=%s sid=%s", tenant_id, run_id, message_sid
    )
    return True


def _alert_report_send_failure(tenant_id: UUID | str, run_id: UUID | str | None) -> None:
    """Fire the ``outbound_failure`` critical alert for a synchronous outcome-report send failure.

    The owner approved and the campaign sent, but the confirmation back to the owner did not go
    out — the exact "owner un-notified" class the outbound_failure detector exists for. Dev-routed
    + PII-scrubbed by dispatch_alert; fully fail-soft (an alert failure must not touch the resume)."""
    try:
        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for

        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        dispatch_alert(Trigger(
            tenant_id=tid,
            trigger_kind="outbound_failure",
            severity=severity_for("outbound_failure"),
            run_id=UUID(str(run_id)) if run_id else None,
            message_text=(
                "Campaign-outcome report to the owner FAILED to send. The campaign executed but "
                "the owner did not get the outcome confirmation — the owner is un-notified. "
                "Investigate the 24h window / owner number."
            ),
            payload={"surface": "campaign_outcome_report"},
        ))
    except Exception as exc:  # noqa: BLE001 — fail-soft: an alert failure must not affect the resume
        logger.warning("VT-562 outcome-report failure-alert dispatch failed (fail-soft): %s", exc)


__all__ = [
    "compose_campaign_outcome_message",
    "maybe_report_campaign_outcome",
    "summary_has_activity",
]
