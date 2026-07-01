"""VT-526 (B3) graph-wiring — the specialist→manager return bridge.

``decide_next_action`` (manager/decision.py) is the pure manager reasoning half; ``SpecialistReturn``
(roster.py) is the documented upward seam. This bridge connects them to the LIVE graph: it parses a
specialist's return envelope (e.g. the ``sales_lane_pushback`` a lane tool emits) into a
``SpecialistReturn`` and runs ``decide_next_action`` on it.

OBSERVE-ONLY (this slice): the resulting ``ManagerDecision`` is recorded to tm_audit
(``event_layer='decides'``) so "specialist pushed back → manager would REVISE/ESCALATE" is a
greppable, real event on live runs — but routing is UNCHANGED (the manager model still drives the
turn). Flipping the decision to actually steer routing is a later, explicitly-gated slice (the
codebase's build-the-rail-then-flip pattern, as with OC1 ``enforce_policy``). Fully fail-soft: an
observability failure never affects the specialist's tool return or the brain's turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # roster pulls langgraph — keep it off the module import surface (dep-less smoke)
    from orchestrator.agent.roster import SpecialistReturn

logger = logging.getLogger(__name__)


def parse_specialist_return(envelope: Any) -> SpecialistReturn | None:
    """Map a specialist return envelope (dict tool-result) → ``SpecialistReturn``. Returns None when
    the envelope is not a recognizable specialist return (so a caller can no-op)."""
    if not isinstance(envelope, dict):
        return None
    # Lazy: constructing SpecialistReturn needs roster (→ langgraph); importing it here (not at
    # module top) keeps ``import specialist_return`` dep-less-safe for the smoke collection.
    from orchestrator.agent.roster import SpecialistReturn

    if envelope.get("pushback"):
        return SpecialistReturn(
            pushback=True,
            reason=str(envelope.get("reason", "") or ""),
            proposed_outcome=str(envelope.get("proposed_outcome", "") or ""),
        )
    action_taken = str(envelope.get("action_taken", "") or "")
    outcome = str(envelope.get("outcome", "") or "")
    if action_taken or outcome:
        return SpecialistReturn(pushback=False, action_taken=action_taken, outcome=outcome)
    return None


def observe_specialist_return(
    envelope: Any, *, agent: str, has_next_step: bool = False
) -> Any | None:
    """Run the manager decision loop on a REAL specialist return + record it to tm_audit
    (OBSERVE-ONLY — no routing change). Returns the ``ManagerDecision`` (for tests / a future
    enforcing caller) or None. Fully fail-soft."""
    try:
        ret = parse_specialist_return(envelope)
        if ret is None:
            return None
        from orchestrator.manager.decision import decide_next_action
        from orchestrator.observability.decorators import _observability_context
        from orchestrator.observability.tm_audit import emit_tm_audit

        decision = decide_next_action(ret, has_next_step=has_next_step)
        ctx = _observability_context.get()
        if ctx is not None:
            emit_tm_audit(
                event_layer="decides",
                event_kind="manager_decision",
                actor="team_manager",
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                summary=(
                    f"specialist {agent!r} returned "
                    f"({'pushback' if ret.pushback else 'action'}); "
                    f"manager decision = {decision.kind.value} (observe-only)"
                ),
                decision={
                    "kind": decision.kind.value,
                    "agent": agent,
                    "pushback": ret.pushback,
                    "reason": decision.reason,
                },
                status="observed",
            )
        return decision
    except Exception:  # noqa: BLE001 — observe-only; must never break the specialist return / turn
        logger.debug("VT-526 observe_specialist_return failed (fail-soft)", exc_info=True)
        return None


__all__ = ["parse_specialist_return", "observe_specialist_return"]
