"""State invariants for the phase machine (VT-3.2).

Deterministic post-transition checks (Pillar 1 — no LLM). check_invariants is
called from inside apply_transition's @DBOS.step transaction, after the
phase_transitions row is written; a violation raises InvariantViolationError,
which rolls the transaction back so the checkpoint is never committed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from orchestrator.state import MAX_TRIAL_EXTENSIONS, SubscriberState


class InvariantViolationError(RuntimeError):
    """Raised when a post-transition state invariant is violated."""

    def __init__(self, invariant: str, detail: str) -> None:
        super().__init__(f"invariant '{invariant}' violated: {detail}")
        self.invariant = invariant
        self.detail = detail


def check_invariants(
    state: SubscriberState, conn: Any, current_transition_id: UUID
) -> None:
    """Raise InvariantViolationError if `state` violates any invariant.

    `conn` is the open transaction connection; SQL checks run inside it so they
    see the just-written phase_transitions row. `current_transition_id` is that
    row's id — excluded from the monotonic check so it does not compare to self.
    """
    phase = state["phase"]

    # 1. Paid phases require a recorded paid conversion.
    if phase in ("paid_active", "paid_at_risk") and state["paid_conversion_at"] is None:
        raise InvariantViolationError(
            "paid_requires_conversion",
            f"phase '{phase}' but paid_conversion_at is None",
        )

    # 2. Trial extensions are capped.
    if state["trial_extension_count"] > MAX_TRIAL_EXTENSIONS:
        raise InvariantViolationError(
            "trial_extension_cap",
            f"trial_extension_count {state['trial_extension_count']} "
            f"exceeds {MAX_TRIAL_EXTENSIONS}",
        )

    # 3. 'refunded' requires a day39_refund_triggered event for this tenant.
    if phase == "refunded":
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM phase_transitions "
            "WHERE tenant_id = %s AND event = 'day39_refund_triggered') AS refund_exists",
            (str(state["tenant_id"]),),
        ).fetchone()
        if not (row and row["refund_exists"]):
            raise InvariantViolationError(
                "refunded_requires_day39",
                "phase 'refunded' but no day39_refund_triggered event recorded",
            )

    # 4. phase_entered_at is monotonic non-decreasing for this tenant.
    row = conn.execute(
        "SELECT MAX(transition_at) AS prior_max FROM phase_transitions "
        "WHERE tenant_id = %s AND id <> %s",
        (str(state["tenant_id"]), str(current_transition_id)),
    ).fetchone()
    prior_max = row["prior_max"] if row else None
    if prior_max is not None and state["phase_entered_at"] < prior_max:
        raise InvariantViolationError(
            "phase_entered_at_monotonic",
            f"phase_entered_at {state['phase_entered_at']} precedes prior {prior_max}",
        )
