"""VT-606 amendment A3 — 24h-window stale-resume re-engagement.

"The loop owns stale resumes: a pause older than the WhatsApp freeform window re-engages via an
approved template (registry SID, never hard-coded), then resumes the exact task/step."

WhatsApp's owner-care freeform window closes 24h after the owner's LAST inbound message; a
freeform send outside it fails Twilio 63016. The system sends an approved out-of-window owner
template to re-open the window before resuming.

VT-683 point B (Fazal 2026-07-22): ``team_reengage`` is MERGED INTO the daily wake-up — this call
site now sends ``team_wakeup2`` (the ONE re-engage surface). Same ``owner_name`` var; the extra
``pending_count`` var is the tenant's queued owner-comms count, floored to 1 (a re-engage is always
about at least one pending matter). The dispatch goes through the SHARED wake-up helper
``owner_surface.wakeup.send_wakeup`` (→ ``owner_send.send_owner_template`` → the ledgered
``twilio_send.send_template_message`` → registry resolve). No new send mechanism, no hardcoded SID;
an unconfigured/misapproved template fails closed to a VTR incident, never a freeform send that
would 63016. ``team_reengage`` is deprecated (kept for history/back-compat resolution).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.utils.twilio_send import SendResult

logger = logging.getLogger("orchestrator.manager.stale_resume")

_TWENTY_FOUR_HOURS = timedelta(hours=24)


def last_owner_inbound_at(tenant_id: UUID | str) -> datetime | None:
    """The tenant's most recent OWNER-authored turn. VT-683 P1: delegates to the ONE
    session-window truth (``owner_surface.session_window``) — this module proved the logic
    first (VT-671-era) and now re-imports it; never re-derive."""
    from orchestrator.owner_surface import session_window

    return session_window.last_owner_inbound_at(tenant_id)


def is_stale(last_inbound_at: datetime | None, *, now: datetime | None = None) -> bool:
    """True iff the WhatsApp freeform window is CLOSED — the inverse of
    ``session_window.window_open`` (one definition, VT-683 P1)."""
    from orchestrator.owner_surface import session_window

    return not session_window.window_open(last_inbound_at, now=now)


def reengage_stale_task(
    tenant_id: UUID | str,
    task_id: UUID | str,
    *,
    owner_phone: str,
    owner_name: str = "",
) -> SendResult | None:
    """Send the approved ``team_wakeup2`` template to re-open the window before the loop resumes the
    exact task/step (VT-683 point B: ``team_reengage`` merged into the wake-up).

    Dispatch goes through the SHARED wake-up helper ``owner_surface.wakeup.send_wakeup`` — the SAME
    ledgered owner-template seam every wake-up uses (registry resolve + ``validate_params`` +
    ``twilio_send``'s template path), not a hand-built params dict. ``pending_count`` is the tenant's
    still-queued owner-comms count, floored to 1 (a re-engage is always about at least one pending
    matter — the copy reads "{{2}} item(s) waiting"). Language routes off the owner's real locale
    inside the helper (en/hi/hing).

    Returns the ``SendResult`` on a real send attempt (success OR a reported failure — Pillar 7
    honesty, never swallowed); returns ``None`` (and raises no exception) when the template itself
    is not configured/approved/signature-mismatched — the caller must treat that as a VTR incident,
    never fall back to a freeform send (which would 63016 outside the window).
    """
    from orchestrator.owner_surface import wakeup

    pending_count = wakeup.queued_comms_count(tenant_id)
    result = wakeup.send_wakeup(
        tenant_id,
        owner_phone=owner_phone,
        owner_name=owner_name,
        pending_count=pending_count,
    )
    if result is None:
        # team_wakeup2 unconfigured / language-variant absent / signature-mismatched — fail CLOSED
        # to a VTR incident, never a freeform send (which would 63016 outside the window).
        logger.warning(
            "stale_resume: %s not configured (fail-closed, no send) tenant=%s task=%s",
            wakeup.WAKEUP_TEMPLATE, tenant_id, task_id,
        )
        _raise_stale_resume_incident(tenant_id, task_id, reason="wakeup_template_unconfigured")
        return None

    emit_tm_audit(
        event_layer="does",
        event_kind="stale_resume_reengage",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"stale-resume wake-up sent for task={task_id} success={result.success}",
        decision={
            "task_id": str(task_id),
            "template_name": wakeup.WAKEUP_TEMPLATE,
            "success": result.success,
        },
    )
    if not result.success:
        _raise_stale_resume_incident(
            tenant_id, task_id, reason=f"send_failed:{result.error_code}:{result.error_message}"
        )
    return result


def _raise_stale_resume_incident(tenant_id: UUID | str, task_id: UUID | str, *, reason: str) -> None:
    """A stale-resume re-engagement that could not be sent must never fail silently — a VTR
    incident (task_id as the soft run_id correlation key, mirrors manager/review.py's usage)."""
    iid = create_incident(
        tenant_id,
        incident_kind="owner_unreachable",
        run_id=task_id,
        severity="warning",
        detail={"source": "stale_resume", "task_id": str(task_id), "reason": reason},
    )
    if iid is not None:
        escalate_incident(tenant_id, iid, to_tier=1, owner_contacted=False)


__all__ = ["is_stale", "last_owner_inbound_at", "reengage_stale_task"]
