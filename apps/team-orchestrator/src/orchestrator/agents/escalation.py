"""VT-474 A3 — the deterministic owner-ESCALATION triggers (extreme-scenario gate).

The A3 ruling (design §8): escalation = CONCRETE deterministic triggers, NOT the brain's vibe. The
team RUNS the business autonomously (§6); the owner is reached ONLY in extreme scenarios, WhatsApp-
only, concise. This module is the pure decision (``should_escalate``) over concrete triggers + the
owner-notify seam (``escalate_owner``) that REUSES the existing owner-targeted send.

THE TRIGGERS (design §8 — each a concrete, machine-checkable condition; the CALLER supplies the
counts/context, the guard decides; zero LLM — Pillar 1):
  - REPEATED_RAIL_TRIP        — a rail (consent/cap/policy) tripped >= N times in a window.
  - SPEND_ANOMALY             — spend in the window exceeds a multiple of the baseline.
  - VOLUME_ANOMALY            — send volume in the window exceeds a multiple of the baseline.
  - OUT_OF_POLICY_IRREVERSIBLE— an OUT_OF_POLICY attempt at an IRREVERSIBLE action (spend/commit).
  - COMPLAINT_SURGE           — complaints in the window >= the surge threshold.
  - OPT_OUT_SURGE             — opt-outs in the window >= the surge threshold.
  - REPEATED_SPECIALIST_FAILURE — a specialist failed >= N times in a window.
  - MONEY_MOVEMENT_REQUEST    — any money-movement / return-filing request (ALWAYS escalates — these
                                are never autonomous in v1; design §8 Finance ADVISORY / Accounting
                                PREPARE-only).
  - SEND_QUALITY_FLAG         — a send-quality flag was raised (signature drift / template misuse).

ORDER MATTERS only for which reason is REPORTED first; ANY trigger escalates. The thresholds are
DATA (module constants, fail-closed conservative), not the brain's choice — a future tenant override
rides a config, never an LLM.

FAIL-CLOSED posture: a trigger fires on the threshold being MET (>=), and the always-escalate triggers
(money-movement, out-of-policy-irreversible) fire on the boolean alone — when in doubt, escalate. The
opposite error (silently NOT escalating an extreme scenario) is the unacceptable one.

CL-390: IDs + reason CODE + numeric counts only in logs/results — never an owner secret, never a
customer phone/name, never a free-form instruction body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# ===========================================================================
# The trigger taxonomy
# ===========================================================================


class EscalationReason(str, Enum):
    """The concrete, deterministic reasons the team escalates to the owner (design §8)."""

    REPEATED_RAIL_TRIP = "repeated_rail_trip"
    SPEND_ANOMALY = "spend_anomaly"
    VOLUME_ANOMALY = "volume_anomaly"
    OUT_OF_POLICY_IRREVERSIBLE = "out_of_policy_irreversible"
    COMPLAINT_SURGE = "complaint_surge"
    OPT_OUT_SURGE = "opt_out_surge"
    REPEATED_SPECIALIST_FAILURE = "repeated_specialist_failure"
    MONEY_MOVEMENT_REQUEST = "money_movement_request"
    SEND_QUALITY_FLAG = "send_quality_flag"


# --- the deterministic thresholds (DATA, fail-closed conservative; never the brain's choice) ---
RAIL_TRIP_THRESHOLD = 3              # >= this many rail trips in the window → escalate
ANOMALY_BASELINE_MULTIPLE = 3.0      # window value > baseline * this → anomaly
COMPLAINT_SURGE_THRESHOLD = 2        # >= this many complaints in the window → escalate
OPT_OUT_SURGE_THRESHOLD = 3          # >= this many opt-outs in the window → escalate
SPECIALIST_FAILURE_THRESHOLD = 3     # >= this many specialist failures in the window → escalate

# The owner-targeted escalation template (registry; owner_name / stuck_on / context). REUSE — it is
# the existing owner-facing "the team needs your eyes" surface (.viabe/templates.md).
ESCALATION_TEMPLATE = "team_agent_stuck_escalation"


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """The deterministic escalation outcome. PII-safe (reason CODE + a numeric detail only).

    ``reason`` is None when NOTHING triggered (the team keeps running autonomously). ``detail`` is a
    small PII-safe numeric/string marker (a count, a multiple) for the audit log + the owner notice
    ``context`` — never a customer fact, never a free-form instruction body."""

    reason: EscalationReason | None
    detail: str = ""

    @property
    def should_escalate(self) -> bool:
        return self.reason is not None


def _anomaly(window_value: float | int | None, baseline: float | int | None) -> bool:
    """A window value is anomalous iff it exceeds ``baseline * ANOMALY_BASELINE_MULTIPLE``. A NULL/0
    baseline with a positive window value is NOT auto-anomalous here (a first-ever spend has no
    baseline) — those route through the policy/spend-ceiling rail (A2), not the anomaly trigger; this
    keeps the anomaly trigger from firing on every cold-start tenant. A NULL window value is not an
    anomaly (no data)."""
    if window_value is None or baseline is None:
        return False
    try:
        wv = float(window_value)
        bl = float(baseline)
    except (TypeError, ValueError):
        return False
    if bl <= 0:
        return False
    return wv > bl * ANOMALY_BASELINE_MULTIPLE


def should_escalate(
    tenant_id: UUID | str,
    context: dict[str, Any] | None = None,
) -> EscalationDecision:
    """The PURE deterministic escalation decision (no I/O — testable in isolation, the A3 core).

    ``context`` is the machine-checkable event/state the CALLER supplies (the action paths assemble
    it from their counters; this module never reads the brain's judgment). Recognized keys (all
    optional; absent ⇒ that trigger does not fire):

      - ``rail_trip_count``            (int) — rail trips in the window.
      - ``spend_window_minor`` + ``spend_baseline_minor`` (int) — spend anomaly inputs.
      - ``volume_window`` + ``volume_baseline`` (int) — send-volume anomaly inputs.
      - ``out_of_policy_irreversible`` (bool) — an OUT_OF_POLICY attempt at an irreversible action.
      - ``complaint_count``            (int) — complaints in the window.
      - ``opt_out_count``              (int) — opt-outs in the window.
      - ``specialist_failure_count``   (int) — specialist failures in the window.
      - ``money_movement_request``     (bool) — a money-movement / return-filing request (ALWAYS).
      - ``send_quality_flag``          (bool) — a send-quality flag.

    Evaluated in a FIXED order (the first satisfied trigger is reported; ANY trigger escalates). The
    ALWAYS-escalate triggers (money-movement, out-of-policy-irreversible) are checked first — they are
    the highest-stakes extreme scenarios. Returns ``EscalationDecision(reason=None)`` when nothing
    triggered (steady-state autonomy — the owner is NOT pestered).
    """
    ctx = context or {}

    def _as_int(key: str, default: int = 0) -> int:
        try:
            return int(ctx.get(key, default))
        except (TypeError, ValueError):
            return default

    # --- ALWAYS-escalate (highest stakes; the boolean alone fires) ---
    if bool(ctx.get("money_movement_request")):
        return _log_and_return(tenant_id, EscalationReason.MONEY_MOVEMENT_REQUEST, "requested")
    if bool(ctx.get("out_of_policy_irreversible")):
        return _log_and_return(tenant_id, EscalationReason.OUT_OF_POLICY_IRREVERSIBLE, "attempted")

    # --- complaint / opt-out surges (DPDP-adjacent; high stakes) ---
    complaints = _as_int("complaint_count")
    if complaints >= COMPLAINT_SURGE_THRESHOLD:
        return _log_and_return(tenant_id, EscalationReason.COMPLAINT_SURGE, str(complaints))
    opt_outs = _as_int("opt_out_count")
    if opt_outs >= OPT_OUT_SURGE_THRESHOLD:
        return _log_and_return(tenant_id, EscalationReason.OPT_OUT_SURGE, str(opt_outs))

    # --- repeated rail trips / specialist failures (a stuck or misbehaving lane) ---
    rail_trips = _as_int("rail_trip_count")
    if rail_trips >= RAIL_TRIP_THRESHOLD:
        return _log_and_return(tenant_id, EscalationReason.REPEATED_RAIL_TRIP, str(rail_trips))
    failures = _as_int("specialist_failure_count")
    if failures >= SPECIALIST_FAILURE_THRESHOLD:
        return _log_and_return(tenant_id, EscalationReason.REPEATED_SPECIALIST_FAILURE, str(failures))

    # --- spend / volume anomalies vs baseline ---
    if _anomaly(ctx.get("spend_window_minor"), ctx.get("spend_baseline_minor")):
        return _log_and_return(tenant_id, EscalationReason.SPEND_ANOMALY, "anomaly")
    if _anomaly(ctx.get("volume_window"), ctx.get("volume_baseline")):
        return _log_and_return(tenant_id, EscalationReason.VOLUME_ANOMALY, "anomaly")

    # --- send-quality flag ---
    if bool(ctx.get("send_quality_flag")):
        return _log_and_return(tenant_id, EscalationReason.SEND_QUALITY_FLAG, "flagged")

    return EscalationDecision(reason=None)


def _log_and_return(
    tenant_id: UUID | str, reason: EscalationReason, detail: str
) -> EscalationDecision:
    logger.info(
        "escalation: should_escalate tenant=%s reason=%s detail=%s",
        str(tenant_id), reason.value, detail,
    )
    return EscalationDecision(reason=reason, detail=detail)


# ===========================================================================
# THE OWNER-NOTIFY SEAM — REUSE the existing owner-targeted send (no fork)
# ===========================================================================


def escalate_owner(
    tenant_id: UUID | str,
    decision: EscalationDecision,
    *,
    stuck_on: str = "an extreme business scenario",
    send_fn: Any | None = None,
) -> str | None:
    """Notify the OWNER of an escalation, WhatsApp-only + concise (design §6/§8).

    REUSES the existing owner-targeted send path (``twilio_send.send_template_message`` to the owner's
    phone — the SAME primitive ``l3_hold._send_presend_notice`` + ``request_owner_approval`` use; an
    OWNER send, ``is_customer_send=False``, so it is exempt from the customer-send choke by design).
    Sends ``team_agent_stuck_escalation`` (owner_name / stuck_on / context). Concise + PII-safe: the
    ``context`` carries the reason CODE + a numeric detail only — never a customer fact or a free-form
    instruction body (CL-390).

    Returns the message SID on success, else None. Best-effort: a send failure is logged and returns
    None (the escalation DECISION already fired deterministically; the notify is the delivery leg). A
    no-op when ``decision.reason`` is None (nothing to escalate). ``send_fn`` injects the transport for
    tests.
    """
    if not decision.should_escalate or decision.reason is None:
        return None

    tid = str(tenant_id)
    reason = decision.reason

    from orchestrator.agents.approval_glue import _owner_display_name
    from orchestrator.db import tenant_connection

    with tenant_connection(tid) as c:
        owner_name = _owner_display_name(c, tid)

    # PII-safe context: the reason code + the numeric detail ONLY.
    context_line = f"{reason.value} ({decision.detail})" if decision.detail else reason.value
    params = {"owner_name": owner_name, "stuck_on": stuck_on, "context": context_line}

    try:
        sender = send_fn or _default_escalation_sender
        result = sender(tid, params)
    except Exception:  # noqa: BLE001 — a notify failure never unwinds the deterministic decision
        logger.warning(
            "escalation: owner notify raised tenant=%s reason=%s", tid, reason.value, exc_info=True
        )
        return None

    sid = getattr(result, "message_sid", None) or (
        result.get("message_sid") if isinstance(result, dict) else None
    )
    success = getattr(result, "success", None)
    if success is None and isinstance(result, dict):
        success = result.get("success")
    if not sid or success is False:
        logger.warning(
            "escalation: owner notify unsuccessful tenant=%s reason=%s", tid, reason.value
        )
        return None

    _emit(tid, reason, decision.detail)
    logger.info("escalation: owner notified tenant=%s reason=%s sid=%s", tid, reason.value, sid)
    return str(sid)


def _default_escalation_sender(tenant_id: str, params: dict[str, str]) -> Any:
    """Live owner-targeted escalation send (lazy import — heavy twilio chain). OWNER send
    (is_customer_send defaults False) — exempt from the customer-send choke by design."""
    from orchestrator.utils.twilio_send import send_template_message

    return send_template_message(UUID(tenant_id), ESCALATION_TEMPLATE, params)


def _emit(tenant_id: str, reason: EscalationReason, detail: str) -> None:
    """Best-effort observability — never fails the caller."""
    try:
        from orchestrator.observability.log import log_event

        log_event(
            event_type="owner_escalation",
            run_id=uuid4(),
            tenant_id=UUID(tenant_id),
            severity="warning",
            component="agents",
            payload={"tenant_id": tenant_id, "reason": reason.value, "detail": detail},
        )
    except Exception:  # noqa: BLE001
        logger.exception("escalation: emit failed tenant=%s", tenant_id)


__all__ = [
    "EscalationReason",
    "EscalationDecision",
    "RAIL_TRIP_THRESHOLD",
    "ANOMALY_BASELINE_MULTIPLE",
    "COMPLAINT_SURGE_THRESHOLD",
    "OPT_OUT_SURGE_THRESHOLD",
    "SPECIALIST_FAILURE_THRESHOLD",
    "ESCALATION_TEMPLATE",
    "should_escalate",
    "escalate_owner",
]
