"""Phase-transition state machine (VT-3.2).

Deterministic (Pillar 1 — no LLM, no reasoning). Pillar 8: phase logic lives
ONLY here. apply_transition is the SOLE public mutator of phase — the
orchestrator-agent and specialists read phase but must never import this
module (CI enforces).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from dbos import DBOS

from orchestrator.db import tenant_connection
from orchestrator.invariants import check_invariants
from orchestrator.state import Phase, SubscriberState

logger = logging.getLogger(__name__)

# VT-365 (Fazal 2026-06-09): 30-day free trial, NO card in trial, opt-in subscribe at/after day 30,
# NO auto-charge, NO refund ever. The refund subsystem (day-39 2x-or-refund) + trial extensions are
# REMOVED. Trial ends in exactly two ways: an explicit owner `subscribe`, or a `trial_expired` to the
# dormant, re-subscribable `lapsed` phase.
ALL_EVENTS: tuple[str, ...] = (
    "signup",
    "subscribe",  # VT-365: explicit owner subscribe (at/after day 30) — the ONLY path to paid_active
    "trial_expired",  # VT-365: 30-day trial elapsed without subscribe -> lapsed (dormant)
    "weekly_low_engagement",
    "payment_failed",  # VT-89: 3rd consecutive Razorpay payment.failed -> paid_at_risk
    "engagement_recovered",
    "cancellation_requested",
    "manual_cancel",
)

# (from_phase, event) -> to_phase. 'cancelled' is terminal. 'lapsed' is dormant but re-subscribable.
TRANSITIONS: dict[tuple[Phase, str], Phase] = {
    ("onboarding", "signup"): "trial",
    # Trial — explicit subscribe (no card/auto-charge) or expire to dormant lapsed.
    ("trial", "subscribe"): "paid_active",
    ("trial", "trial_expired"): "lapsed",
    ("trial", "cancellation_requested"): "cancelled",
    ("trial", "manual_cancel"): "cancelled",
    # Lapsed — dormant/read-only, re-subscribable any time.
    ("lapsed", "subscribe"): "paid_active",
    ("lapsed", "cancellation_requested"): "cancelled",
    ("lapsed", "manual_cancel"): "cancelled",
    # Paid — engagement risk + cancellation (NO day-39 refund path).
    ("paid_active", "weekly_low_engagement"): "paid_at_risk",
    ("paid_active", "cancellation_requested"): "cancelled",
    ("paid_active", "manual_cancel"): "cancelled",
    ("paid_at_risk", "engagement_recovered"): "paid_active",
    ("paid_at_risk", "cancellation_requested"): "cancelled",
    ("paid_at_risk", "manual_cancel"): "cancelled",
    # VT-89: 3rd consecutive Razorpay payment.failed -> paid_at_risk (self-loop = idempotent).
    ("paid_active", "payment_failed"): "paid_at_risk",
    ("paid_at_risk", "payment_failed"): "paid_at_risk",
}


class InvalidTransitionError(RuntimeError):
    """Raised when (from_phase, event) is not a permitted transition."""

    def __init__(self, from_phase: str, event: str, to_phase: str | None) -> None:
        super().__init__(
            f"invalid transition: ({from_phase!r}, {event!r}) -> {to_phase!r}"
        )
        self.from_phase = from_phase
        self.event = event
        self.to_phase = to_phase


class VerificationRequiredError(InvalidTransitionError):
    """VT-361 gate: card_captured → paid_active blocked because business verification is below
    gstin_verified. A DISTINCT type (not a generic InvalidTransition) so the payment-capture caller
    can surface a clear owner-facing "verification pending" state, not a silent stall."""


# VT-361: the activation gate threshold. gstin_verified is the floor; vtr_verified is above it.
_ACTIVATION_VERIFIED_TIERS = frozenset({"gstin_verified", "vtr_verified"})


def _activation_verification_ok(tenant_id: object) -> bool:
    """Read verification_status SERVER-SIDE from the tenant row (never a client field — the IDOR /
    forward-raw-body lesson). Fail-closed: a missing row / read error → not verified."""
    try:
        with tenant_connection(str(tenant_id)) as conn:
            row = conn.execute(
                "SELECT verification_status FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
    except Exception:  # noqa: BLE001 — fail-closed on any read error (vendor/DB)
        logger.exception("VT-361 activation gate: verification read failed tenant=%s", tenant_id)
        return False
    if row is None:
        return False
    status = row["verification_status"] if isinstance(row, dict) else row[0]
    return status in _ACTIVATION_VERIFIED_TIERS


def _resolve(state: SubscriberState, event: str) -> Phase:
    """Return the to_phase for (state.phase, event), or raise."""
    from_phase = state["phase"]
    to_phase = TRANSITIONS.get((from_phase, event))
    if to_phase is None:
        raise InvalidTransitionError(from_phase, event, None)
    # VT-361/VT-365 activation gate: a tenant cannot reach paid_active below gstin_verified. The gate
    # is on the explicit `subscribe` (the ONLY path to paid_active now), NOT on trial entry — a legit
    # verified owner subscribes at day 30; a GSTIN-less business is blocked at subscribe, not earlier.
    if event == "subscribe" and to_phase == "paid_active" and not _activation_verification_ok(
        state["tenant_id"]
    ):
        raise VerificationRequiredError(from_phase, event, to_phase)
    return to_phase


@DBOS.step()
def apply_transition(
    state: SubscriberState, event: str, context: dict
) -> SubscriberState:
    """Apply `event` to `state` and return the new state. Sole phase mutator.

    Atomic within this @DBOS.step: the phase_transitions row, the tenants.phase
    mirror, and the invariant checks all run in one DB transaction. An invariant
    violation rolls the transaction back — the checkpoint is never committed.
    """
    from_phase = state["phase"]
    to_phase = _resolve(state, event)
    now = datetime.now(UTC)

    new_state: SubscriberState = {**state, "phase": to_phase, "phase_entered_at": now}
    if event == "signup":
        new_state["trial_started_at"] = now
    elif event == "subscribe":
        new_state["paid_conversion_at"] = now
    new_state["history"] = [
        *state["history"],
        {"event": event, "from": from_phase, "to": to_phase, "at": now.isoformat()},
    ]

    with tenant_connection(state["tenant_id"]) as conn, conn.transaction():
        row = conn.execute(
            "INSERT INTO phase_transitions "
            "(tenant_id, from_phase, to_phase, event, transition_at, reason, run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                str(state["tenant_id"]),
                from_phase,
                to_phase,
                event,
                now,
                context.get("reason"),
                str(state["run_id"]),
            ),
        ).fetchone()
        # Mirror phase onto the tenants table (denormalised; not source of truth).
        conn.execute(
            "UPDATE tenants SET phase = %s, phase_entered_at = %s WHERE id = %s",
            (to_phase, now, str(state["tenant_id"])),
        )
        # TODO VT-122: also emit an observability step_record to pipeline_steps
        # via the @observability.step decorator when it ships.
        # Invariants run before commit — a violation rolls back this transaction.
        check_invariants(new_state, conn, row["id"])

        # VT-309: L2 episodic phase_transitioned, IN THIS TXN (atomic with the
        # phase_transitions INSERT — an invariant rollback drops it too).
        from orchestrator.knowledge.l2_types import L2EventType
        from orchestrator.knowledge.l2_writer import (
            deterministic_event_id,
            record_episodic_event,
        )

        record_episodic_event(
            state["tenant_id"],
            L2EventType.PHASE_TRANSITIONED,
            payload={
                "from_phase": from_phase,
                "to_phase": to_phase,
                "event": event,
                "run_id": str(state["run_id"]),
            },
            referenced_entity_type="tenant",
            referenced_entity_id=state["tenant_id"],
            event_id=deterministic_event_id(
                state["tenant_id"], L2EventType.PHASE_TRANSITIONED, row["id"]
            ),
            conn=conn,
        )

    # VT-333: post-transition AUDIT-ONLY founding-slot release on a cancelled transition. Runs
    # AFTER the deterministic txn commits (NOT in apply_transition's core) on the SERVICE-role
    # pool — founding_tier_claims has no app_role UPDATE policy, so the RLS tenant_connection
    # above cannot touch it. Best-effort + audit-only: a missed release just leaves released_at
    # NULL, NEVER decrements the counter (no-reopen policy → zero integrity risk; Cowork
    # 20260605T143300Z). Mirrors the VT-94 refund-path release.
    if to_phase == "cancelled":
        try:
            from orchestrator.billing.founding_counter import release_founding_slot
            from orchestrator.graph import get_pool

            with get_pool().connection() as _fc_conn:
                release_founding_slot(_fc_conn, state["tenant_id"])
        except Exception:
            logger.exception(
                "VT-333 founding-slot release failed (audit-only) tenant=%s",
                state["tenant_id"],
            )

    return new_state
