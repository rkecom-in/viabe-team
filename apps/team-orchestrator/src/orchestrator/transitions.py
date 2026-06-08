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
from orchestrator.state import MAX_TRIAL_EXTENSIONS, Phase, SubscriberState

logger = logging.getLogger(__name__)

# The 12 lifecycle events this machine consumes (event sources: VT-3.3 / 3.5 / 3.9).
ALL_EVENTS: tuple[str, ...] = (
    "signup",
    "card_captured",
    "trial_extension_granted",
    "trial_extension_exhausted",
    "weekly_low_engagement",
    "payment_failed",  # VT-89: 3rd consecutive Razorpay payment.failed -> paid_at_risk
    "engagement_recovered",
    "cancellation_requested",
    "day39_refund_offered",  # VT-85: day-39 refund OFFER (parks in refund_offered)
    "day39_refund_triggered",
    "day39_continue",
    "manual_cancel",
)

# (from_phase, event) -> to_phase. 'cancelled' and 'refunded' are terminal —
# they have no outgoing transitions.
TRANSITIONS: dict[tuple[Phase, str], Phase] = {
    ("onboarding", "signup"): "trial",
    # Trial (and extended trial) — convert, extend, or end.
    ("trial", "card_captured"): "paid_active",
    ("trial", "trial_extension_granted"): "trial_extended",
    ("trial", "trial_extension_exhausted"): "cancelled",
    ("trial", "cancellation_requested"): "cancelled",
    ("trial", "manual_cancel"): "cancelled",
    ("trial_extended", "card_captured"): "paid_active",
    ("trial_extended", "trial_extension_granted"): "trial_extended",
    ("trial_extended", "trial_extension_exhausted"): "cancelled",
    ("trial_extended", "cancellation_requested"): "cancelled",
    ("trial_extended", "manual_cancel"): "cancelled",
    # Paid — engagement risk, day-39 evaluation, cancellation.
    ("paid_active", "weekly_low_engagement"): "paid_at_risk",
    ("paid_active", "day39_continue"): "paid_active",
    ("paid_active", "day39_refund_triggered"): "refunded",
    ("paid_active", "cancellation_requested"): "cancelled",
    ("paid_active", "manual_cancel"): "cancelled",
    ("paid_at_risk", "engagement_recovered"): "paid_active",
    ("paid_at_risk", "day39_continue"): "paid_active",
    ("paid_at_risk", "day39_refund_triggered"): "refunded",
    ("paid_at_risk", "cancellation_requested"): "cancelled",
    ("paid_at_risk", "manual_cancel"): "cancelled",
    # VT-89: 3rd consecutive Razorpay payment.failed -> paid_at_risk. The
    # paid_at_risk self-loop keeps a 4th+ failure from raising (idempotent).
    ("paid_active", "payment_failed"): "paid_at_risk",
    ("paid_at_risk", "payment_failed"): "paid_at_risk",
    # VT-85 day-39 refund OFFER (Pillar 7 — no auto-refund). The evaluator parks
    # the tenant in refund_offered; the owner's REFUND/CONTINUE reply or the 48h
    # timeout resolves it. The direct (paid_*, day39_refund_triggered) paths above
    # stay for the manual_request refund (Fazal ops) — refund_offered is reached
    # only via the offer.
    ("paid_active", "day39_refund_offered"): "refund_offered",
    ("paid_at_risk", "day39_refund_offered"): "refund_offered",
    ("refund_offered", "day39_refund_triggered"): "refunded",  # REFUND reply -> execute_refund
    ("refund_offered", "day39_continue"): "paid_active",  # CONTINUE reply / 48h timeout
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
    # Deterministic precondition: trial extensions are capped.
    if (
        event == "trial_extension_granted"
        and state["trial_extension_count"] >= MAX_TRIAL_EXTENSIONS
    ):
        raise InvalidTransitionError(from_phase, event, to_phase)
    # VT-361 activation gate (Fazal 2026-06-08): a tenant cannot reach paid_active below
    # gstin_verified. GSTIN-less businesses cannot activate — intended. Server-side DB read.
    if event == "card_captured" and to_phase == "paid_active" and not _activation_verification_ok(
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
    elif event == "trial_extension_granted":
        new_state["trial_extension_count"] = state["trial_extension_count"] + 1
    elif event == "card_captured":
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
        # VT-93: anchor the 30-day graceful-exit window atomically with the phase
        # flip (graceful_exit.portal_access_allowed reads tenants.refunded_at; the
        # atomic set avoids a phase=refunded / refunded_at=NULL window).
        if event == "day39_refund_triggered":
            conn.execute(
                "UPDATE tenants SET refunded_at = %s WHERE id = %s",
                (now, str(state["tenant_id"])),
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
