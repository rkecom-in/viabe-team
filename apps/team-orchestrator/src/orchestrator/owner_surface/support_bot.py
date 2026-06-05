"""VT-88 — SupportBot escalation fallback (Phase 1).

Closes the SILENT-DROP: when the agent dispatch terminates UNRESOLVED (aborted_hard_limit /
escalated), the owner gets an ack ("a human will follow up") — NEVER silence
(Pillar 7). The 2nd+ unresolved run in 24h ALSO escalates to Fazal.

Safety (Cowork-locked): the escalate trigger is DETERMINISTIC — a SQL counter (a DB fact) +
the terminal status. An LLM confidence may ADD an escalate, NEVER suppress one — we fail
TOWARD giving the owner a human. PII-safe alert: tenant + owner-phone LAST-4 + run_id only;
the raw owner message stays at rest (Fazal opens the run by id in the Ops Console). CL-390.

DEFERRED (Phase 2 — VT-343): 'completed-no-send' detection (the SAME silent-drop, but needs
per-run send-tracking infra — a LAUNCH-RELEVANT residual, not just nice-to-have), SLA
enforcement, the Fazal /resolve command, the 3rd-escalation fatigue flag, support_resolved.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Dispatch terminals (dispatch.py FinalStatus) that mean the owner got NO resolution.
# 'failed' is NOT a FinalStatus (only completed/escalated/aborted_hard_limit/paused), so it
# is deliberately excluded — including it would be dead code.
_UNRESOLVED = frozenset({"aborted_hard_limit", "escalated"})
_ESCALATE_THRESHOLD = 2  # the 2nd+ unresolved run in the window escalates to Fazal
# NEEDS-FAZAL + LAUNCH-GATE: a Meta-approved template + Hindi. Copy = "Let me get Fazal to
# help — he'll follow up." NO time promise (SLA enforcement is Phase 2; an honest ack,
# Pillar 7). NOTE: until its SID is provisioned the ack send fails-safe → the owner gets NO
# ack, so the no-silence guarantee is INERT. support_handoff MUST be provisioned before
# owner-facing support goes live (VT-89 launch-gate).
_HANDOFF_TEMPLATE = "support_handoff"


def _last4(phone: str | None) -> str:
    if not phone:
        return "?"
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 10 else "?"  # only a full phone yields a real last-4


def _unresolved_count_24h(tenant_id: UUID | str) -> int:
    """Deterministic counter: this tenant's unresolved-terminal runs in the last 24h. Called
    AFTER the current run's status is persisted, so the count INCLUDES it."""
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pipeline_runs "
            "WHERE tenant_id = %s AND status = ANY(%s) "
            "AND started_at > now() - interval '24 hours'",
            (str(tenant_id), list(_UNRESOLVED)),
        ).fetchone()
    return int(dict(row)["n"]) if row else 0


def _send_handoff_ack(tenant_id: UUID | str, sender_phone: str | None, reference: str) -> None:
    from orchestrator.utils.twilio_send import send_template_message

    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    try:
        send_template_message(
            tid, _HANDOFF_TEMPLATE, {"1": reference}, recipient_phone=sender_phone or None
        )
    except Exception:
        logger.exception("VT-88 handoff ack send failed tenant=%s", tenant_id)


def _alert_fazal_safe(tenant_id: UUID | str, sender_phone: str | None, run_id: str) -> None:
    """PII-safe Fazal alert — ids + last-4 only; the raw owner message stays at rest."""
    try:
        from orchestrator.billing.refund_executor import _alert_fazal

        _alert_fazal(
            f"⚠️ SupportBot escalation (VT-88)\n"
            f"tenant={tenant_id}\nowner=****{_last4(sender_phone)}\nrun={run_id}\n"
            f"(2+ unresolved in 24h — open the run in the Ops Console)"
        )
    except Exception:
        logger.exception("VT-88 Fazal alert failed tenant=%s", tenant_id)


def maybe_escalate_support(
    *, tenant_id: UUID | str, run_id: str, event: Any, final_status: str
) -> dict[str, Any]:
    """On an UNRESOLVED terminal: ack the owner (never silence) + escalate to Fazal on the
    2nd+ in 24h. Plain fn (NOT a DBOS step — it invokes the send step); best-effort
    sends/alerts. Returns a small result for tests + observability."""
    if final_status not in _UNRESOLVED:
        return {"action": "none", "reason": "resolved_terminal"}

    sender_phone = getattr(event, "sender_phone", None)

    # 1. Owner ack — ALWAYS on an unresolved terminal (this is the no-silence guarantee).
    _send_handoff_ack(tenant_id, sender_phone, run_id)

    # 2. Deterministic counter (this run's status is already persisted → count includes it).
    count = _unresolved_count_24h(tenant_id)
    if count < _ESCALATE_THRESHOLD:
        return {"action": "ack_only", "unresolved_24h": count}

    # 3. 2nd+ → escalate to Fazal (record + PII-safe alert). record_escalation is idempotent
    # on run_id (one escalation per run), so a re-processed run won't double-escalate.
    from orchestrator.escalations import record_escalation

    try:
        record_escalation(
            tenant_id,
            kind="support_fallback",
            severity="medium",
            run_id=run_id,
            notes="VT-88 owner unresolved-terminal fallback (2+ in 24h)",
        )
    except Exception:
        logger.exception("VT-88 record_escalation failed tenant=%s", tenant_id)
    _alert_fazal_safe(tenant_id, sender_phone, run_id)
    return {"action": "escalated", "unresolved_24h": count}
