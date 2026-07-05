"""VT-606 amendment A3 — 24h-window stale-resume re-engagement.

"The loop owns stale resumes: a pause older than the WhatsApp freeform window re-engages via an
approved template (registry SID, never hard-coded), then resumes the exact task/step."

WhatsApp's owner-care freeform window closes 24h after the owner's LAST inbound message; a
freeform send outside it fails Twilio 63016. ``team_reengage`` (VT-486, Meta-approved UTILITY,
``agent_selectable: false`` — system-invoked only) is the EXISTING approved template built for
exactly this out-of-window owner re-engagement; ``owner_send.send_owner_template`` is the EXISTING
thin, ledgered send seam (VT-393/VT-524) built reusable for "trial_sweep's owner notify" and any
other owner-utility template send — this module is its second real caller. No new send mechanism,
no hardcoded SID (the registry resolves it; an unconfigured/misapproved template fails closed to a
VTR incident, never a freeform send that would 63016).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.owner_surface.owner_send import send_owner_template
from orchestrator.templates_registry import (
    TemplateRegistryError,
    UnknownLanguageVariantError,
    UnknownTemplateError,
)
from orchestrator.utils.twilio_send import SendResult

logger = logging.getLogger("orchestrator.manager.stale_resume")

_REENGAGE_TEMPLATE_NAME = "team_reengage"
_TWENTY_FOUR_HOURS = timedelta(hours=24)


def last_owner_inbound_at(tenant_id: UUID | str) -> datetime | None:
    """The tenant's most recent OWNER-authored ``conversation_log`` turn, or ``None`` if the
    tenant has never messaged (a fresh tenant — treated as stale by ``is_stale``)."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last_at FROM conversation_log "
            "WHERE tenant_id = %s AND role = 'owner'",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    val = row["last_at"] if isinstance(row, dict) else row[0]
    return val if isinstance(val, datetime) else None


def is_stale(last_inbound_at: datetime | None, *, now: datetime | None = None) -> bool:
    """True iff the WhatsApp freeform window is CLOSED (>24h since the owner's last inbound, or
    the owner has never messaged at all)."""
    now = now or datetime.now(timezone.utc)
    if last_inbound_at is None:
        return True
    return (now - last_inbound_at) > _TWENTY_FOUR_HOURS


def reengage_stale_task(
    tenant_id: UUID | str,
    task_id: UUID | str,
    *,
    owner_phone: str,
    owner_name: str = "",
    language: str = "en",
) -> SendResult | None:
    """Send the approved ``team_reengage`` template to re-open the window before the loop resumes
    the exact task/step. Returns the ``SendResult`` on a real send attempt (success OR a reported
    failure — Pillar 7 honesty, never swallowed); returns ``None`` (and raises no exception) when
    the template itself is not configured/approved — the caller must treat that as "emit the
    pending_owner_notification state + a VTR incident," never fall back to a freeform send (which
    would 63016 outside the window).
    """
    try:
        result = send_owner_template(
            UUID(str(tenant_id)),
            _REENGAGE_TEMPLATE_NAME,
            language,
            {"owner_name": owner_name},
            recipient_phone=owner_phone,
        )
    except (UnknownTemplateError, UnknownLanguageVariantError, TemplateRegistryError) as exc:
        logger.warning(
            "stale_resume: team_reengage not configured for language=%s (fail-closed, no send) "
            "tenant=%s task=%s: %s",
            language, tenant_id, task_id, exc,
        )
        _raise_stale_resume_incident(tenant_id, task_id, reason=f"template_unconfigured:{exc}")
        return None

    emit_tm_audit(
        event_layer="does",
        event_kind="stale_resume_reengage",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"stale-resume re-engagement sent for task={task_id} success={result.success}",
        decision={
            "task_id": str(task_id),
            "template_name": _REENGAGE_TEMPLATE_NAME,
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
