"""Phase-transition state machine (VT-3.2).

Deterministic (Pillar 1 — no LLM, no reasoning). Pillar 8: phase logic lives
ONLY here. apply_transition is the SOLE public mutator of phase — the
orchestrator-agent and specialists read phase but must never import this
module (CI enforces).
"""

from __future__ import annotations

from datetime import UTC, datetime

from dbos import DBOS

from orchestrator.graph import get_pool
from orchestrator.invariants import check_invariants
from orchestrator.state import MAX_TRIAL_EXTENSIONS, Phase, SubscriberState

# The 10 lifecycle events this machine consumes (event sources: VT-3.3 / 3.5 / 3.9).
ALL_EVENTS: tuple[str, ...] = (
    "signup",
    "card_captured",
    "trial_extension_granted",
    "trial_extension_exhausted",
    "weekly_low_engagement",
    "engagement_recovered",
    "cancellation_requested",
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

    with get_pool().connection() as conn, conn.transaction():
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

    return new_state
