"""VT-606 amendment A3 — 24h-window stale-resume re-engagement.

"The loop owns stale resumes: a pause older than the WhatsApp freeform window re-engages via an
approved template (registry SID, never hard-coded), then resumes the exact task/step."

WhatsApp's owner-care freeform window closes 24h after the owner's LAST inbound message; a
freeform send outside it fails Twilio 63016. ``team_reengage`` (VT-486, Meta-approved UTILITY,
routed ``reengage: any -> team_reengage`` in ``template_routing.yaml``, ``agent_selectable: false``
— system-invoked only) is the EXISTING approved template built for exactly this out-of-window owner
re-engagement.

Team-lead ruling (VT-606 recon follow-up): reuse the SAME owner-facing send chokepoint
``request_owner_approval.arm_pause_request`` uses for its approval template — registry resolve +
``validate_params`` + ``twilio_send``'s template path — rather than inventing a parallel one. So:
``output_composer.compose_owner_output(None, state, "reengage")`` derives the template name +
params (the SAME deterministic composer every other owner-facing send goes through), the resolved
signature is defensively re-checked via ``templates_registry.validate_params`` (mirroring
``send_whatsapp_template.py``'s own discipline), and the actual dispatch is
``owner_send.send_owner_template`` (VT-393/VT-524's ledgered owner-template seam ->
``twilio_send.send_template_message`` -> registry resolve). No new send mechanism, no hardcoded SID;
an unconfigured/misapproved template fails closed to a VTR incident, never a freeform send that
would 63016.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.output_composer import compose_owner_output
from orchestrator.owner_surface.freeform_acks import resolve_owner_locale
from orchestrator.owner_surface.owner_send import send_owner_template
from orchestrator.templates_registry import (
    TemplateRegistryError,
    UnknownLanguageVariantError,
    UnknownTemplateError,
    VariableSignatureMismatchError,
    validate_params,
)
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
    """Send the approved ``team_reengage`` template to re-open the window before the loop resumes
    the exact task/step.

    Composes via ``output_composer.compose_owner_output(None, state, "reengage")`` (the SAME
    deterministic composer path, not a hand-built params dict) — ``template_routing.yaml``'s
    ``reengage: any -> team_reengage`` entry means this ALWAYS resolves ``team_reengage``
    regardless of tenant phase. Language comes from ``resolve_owner_locale`` (the owner's real
    ``preferred_language``/``language_preference``, not a caller-guessed default). The resolved
    params are re-checked via ``templates_registry.validate_params`` before dispatch (defensive —
    ``_derive_template_params``'s "reengage" branch always emits exactly ``{"owner_name": ...}``,
    matching the template's one-variable signature, but this is the same belt-and-suspenders check
    the customer-send tool applies, never skipped for an owner send either).

    Returns the ``SendResult`` on a real send attempt (success OR a reported failure — Pillar 7
    honesty, never swallowed); returns ``None`` (and raises no exception) when the template itself
    is not configured/approved/signature-mismatched — the caller must treat that as a VTR incident,
    never fall back to a freeform send (which would 63016 outside the window).
    """
    locale = resolve_owner_locale(tenant_id)
    state = {"preferred_language": locale, "owner_name": owner_name}
    composed = compose_owner_output(None, state, "reengage")
    template_name = composed.template_name
    if template_name is None:
        # Structurally unreachable today (the routing table's "reengage: any" entry always
        # resolves) — guarded anyway since a future routing-yaml edit could regress it silently.
        logger.warning(
            "stale_resume: compose_owner_output resolved no template for 'reengage' "
            "(fail-closed, no send) tenant=%s task=%s", tenant_id, task_id,
        )
        _raise_stale_resume_incident(tenant_id, task_id, reason="no_template_resolved")
        return None

    try:
        validate_params(template_name, composed.preferred_language, composed.template_params)
        result = send_owner_template(
            UUID(str(tenant_id)),
            template_name,
            composed.preferred_language,
            composed.template_params,
            recipient_phone=owner_phone,
        )
    except (
        UnknownTemplateError, UnknownLanguageVariantError,
        VariableSignatureMismatchError, TemplateRegistryError,
    ) as exc:
        logger.warning(
            "stale_resume: %s not configured for language=%s (fail-closed, no send) "
            "tenant=%s task=%s: %s",
            template_name, composed.preferred_language, tenant_id, task_id, exc,
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
            "template_name": template_name,
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
