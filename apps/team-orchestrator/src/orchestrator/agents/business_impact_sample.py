"""VT-467 — a SAMPLE business-impact action wired through the rails framework (the proof of wiring).

This is NOT a lane (VT-468-472 build the lanes). It is ONE representative consequential action —
``propose_spend`` — wired end-to-end through ``business_impact_choke`` to DEMONSTRATE the gate +
the structural choke, and to give the non-bypassability proof a concrete callable. The effect itself
is a STUB (``_apply_spend_effect`` does no real money movement) — VT-467 builds the FRAMEWORK + a
sample, not a real payment integration.

The canonical usage pattern every real lane action follows:

    gate = assert_or_gate_business_action(tenant_id, BusinessImpactClass.SPEND, amount_paise, conn=conn)
    if gate.requires_owner_approval:
        return arm_business_action_approval(tenant_id, run_id, gate, summary=..., conn=conn)  # owner gate
    with business_action_context(BusinessImpactClass.SPEND):   # autonomous: enter the gated extent
        _apply_spend_effect(...)                                # the effect (calls the transport guard)

The effect ALWAYS calls ``assert_in_business_action_context`` first — so even the autonomous path is
structurally barred from running the effect without having entered the context (the choke is at the
EFFECT, not just at the call site). A direct caller that skips the gate raises
``UngatedBusinessActionError``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.agents.business_impact_choke import (
    BusinessActionDecision,
    BusinessImpactClass,
    arm_business_action_approval,
    assert_in_business_action_context,
    assert_or_gate_business_action,
    business_action_context,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpendOutcome:
    """The outcome of a propose_spend. PII-safe (ids + amount + decision/marker only)."""

    decision: str                       # BusinessActionDecision value
    executed: bool                      # True iff the effect actually ran (autonomous path)
    amount_paise: int
    reason: str                         # the gate reason code
    approval_id: str | None = None      # set when routed to owner approval
    approval_status: str | None = None  # the PauseRequestResult status, when armed


def _apply_spend_effect(tenant_id: UUID | str, amount_paise: int, *, label: str) -> None:
    """The (stubbed) consequential effect. STRUCTURALLY barred outside the gated context.

    A real lane would call a payment/boost API here; VT-467 stubs it. The FIRST line is the
    transport-level choke: the effect cannot run unless the caller entered
    ``business_action_context(SPEND)`` — so even a bug that calls this directly fails closed.
    """
    assert_in_business_action_context(BusinessImpactClass.SPEND)
    # --- real money movement would go here; STUB for VT-467 ---
    logger.info(
        "business_impact_sample: spend effect applied (stub) tenant=%s amount_paise=%d label=%s",
        str(tenant_id), amount_paise, label,
    )


def propose_spend(
    tenant_id: UUID | str,
    run_id: UUID | str,
    amount_paise: int,
    *,
    label: str = "spend",
    conn: Any = None,
    send_fn: Any | None = None,
    dry_run: bool = False,
) -> SpendOutcome:
    """Propose a SPEND of ``amount_paise``. The ONE code path a spend may take.

    DETERMINISTIC: ``assert_or_gate_business_action`` decides. AUTONOMOUS → run the effect inside the
    gated context. REQUIRES_OWNER_APPROVAL → route through the existing owner-approval machinery
    (``arm_business_action_approval``); the effect does NOT run until the owner approves (a separate
    resume path the lanes wire — out of VT-467 scope). Never raises for a gate decision; the
    transport guard raises only on a structural bypass.
    """
    gate = assert_or_gate_business_action(
        tenant_id, BusinessImpactClass.SPEND, amount_paise, conn=conn
    )

    if gate.requires_owner_approval:
        result = arm_business_action_approval(
            tenant_id,
            run_id,
            gate,
            summary=f"Spend ₹{amount_paise / 100:.2f} ({label}) — approve?",
            conn=conn,
            send_fn=send_fn,
            dry_run=dry_run,
        )
        return SpendOutcome(
            decision=BusinessActionDecision.REQUIRES_OWNER_APPROVAL.value,
            executed=False,
            amount_paise=amount_paise,
            reason=gate.reason,
            approval_id=str(result.approval_id) if getattr(result, "approval_id", None) else None,
            approval_status=getattr(result, "status", None),
        )

    # Autonomous: enter the gated extent, then run the effect (which re-asserts the context).
    with business_action_context(BusinessImpactClass.SPEND):
        _apply_spend_effect(tenant_id, amount_paise, label=label)
    return SpendOutcome(
        decision=BusinessActionDecision.AUTONOMOUS.value,
        executed=True,
        amount_paise=amount_paise,
        reason=gate.reason,
    )


__all__ = ["SpendOutcome", "propose_spend"]
