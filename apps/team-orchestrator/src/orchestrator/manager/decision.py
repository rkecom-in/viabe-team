"""VT-526 (B3) — the manager decision loop over specialist returns + the B2 task spine.

The moat: after a specialist runs, the manager READS its ``SpecialistReturn`` and decides what
happens next — accept, advance to the next planned step, revise-and-re-dispatch, ask the owner,
or escalate. Before this, ``SpecialistReturn`` was defined but NEVER read (the manager just let
the graph terminate); ``decide_next_action`` is that missing consumer, and ``record_decision``
drives the decision onto the B2 ``manager_tasks``/``manager_task_steps`` spine (sequential
advancement) under the CAS guard.

Deterministic + pure decision logic (``decide_next_action``) — testable in isolation, no LLM, no
DB. The live LangGraph wiring that makes a specialist sub-graph EMIT its ``SpecialistReturn`` into
the parent state and calls this from the manager node is the next slice (graph plumbing across the
9 lanes); this module is the reasoning + persistence half that slice will call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # avoid importing the roster (agent/langgraph deps) at runtime — duck-typed below
    from orchestrator.agent.roster import SpecialistReturn


class ManagerDecisionKind(str, Enum):
    ACCEPT = "accept"                    # specialist acted, plan exhausted → verify the task
    NEXT_SPECIALIST = "next_specialist"  # step done → dispatch the next planned step
    REVISE = "revise"                    # pushback with a better outcome → re-dispatch reframed
    CLARIFY = "clarify"                  # nothing actionable → ask the owner
    ESCALATE = "escalate"                # infeasible in-lane, no proposed path → escalate


@dataclass(frozen=True)
class ManagerDecision:
    kind: ManagerDecisionKind
    reason: str
    revised_outcome: str | None = None   # REVISE — the reframed desired_outcome to re-dispatch with


def decide_next_action(ret: "SpecialistReturn", *, has_next_step: bool) -> ManagerDecision:
    """Read a specialist's return and decide the manager's next action — deterministic.

    Branches on the two-way protocol (roster ``SpecialistReturn``):
      - PUSHBACK with a ``proposed_outcome`` → REVISE (re-frame + re-dispatch, never force).
      - PUSHBACK with no proposed path       → ESCALATE (manager can't re-frame in-lane).
      - ACTION with nothing actionable        → CLARIFY (ask the owner).
      - ACTION taken, more steps planned      → NEXT_SPECIALIST (advance the plan).
      - ACTION taken, plan exhausted          → ACCEPT (move the task to verification).
    """
    if ret.pushback:
        proposed = (ret.proposed_outcome or "").strip()
        if proposed:
            return ManagerDecision(
                ManagerDecisionKind.REVISE,
                reason=(ret.reason or "").strip() or "specialist proposed a better outcome",
                revised_outcome=proposed,
            )
        return ManagerDecision(
            ManagerDecisionKind.ESCALATE,
            reason=(ret.reason or "").strip() or "specialist pushed back with no proposed outcome",
        )
    if not (ret.action_taken or "").strip():
        return ManagerDecision(
            ManagerDecisionKind.CLARIFY,
            reason="specialist returned no action and no pushback",
        )
    if has_next_step:
        return ManagerDecision(
            ManagerDecisionKind.NEXT_SPECIALIST, reason="step complete; advancing the plan"
        )
    return ManagerDecision(
        ManagerDecisionKind.ACCEPT, reason="step complete; plan exhausted → verify"
    )


def record_decision(
    tenant_id: UUID | str,
    task_id: UUID | str,
    step_id: UUID | str,
    decision: ManagerDecision,
    *,
    next_step_id: UUID | str | None = None,
) -> None:
    """Translate a ``ManagerDecision`` into B2 task/step CAS transitions (the CAS guard makes each
    transition terminal-safe). NEXT_SPECIALIST advances to ``next_step_id`` when given."""
    from orchestrator.manager import task_store  # lazy — keeps decide_next_action import-light

    kind = decision.kind
    if kind in (ManagerDecisionKind.ACCEPT, ManagerDecisionKind.NEXT_SPECIALIST):
        task_store.set_step_status(
            tenant_id, step_id, "done", expected_from=("pending", "running", "waiting")
        )
        if kind is ManagerDecisionKind.ACCEPT:
            task_store.set_task_status(
                tenant_id, task_id, "verifying", expected_from=("running",)
            )
        elif next_step_id is not None:
            task_store.set_step_status(
                tenant_id, next_step_id, "running", expected_from=("pending",)
            )
            task_store.set_task_status(
                tenant_id, task_id, "running",
                current_step_id=next_step_id,
                expected_from=tuple(task_store.TASK_NON_TERMINAL),
            )
    elif kind is ManagerDecisionKind.REVISE:
        # re-dispatch: the current step returns to pending to re-run with the revised outcome.
        task_store.set_step_status(
            tenant_id, step_id, "pending", expected_from=("running", "waiting")
        )
    elif kind is ManagerDecisionKind.CLARIFY:
        task_store.set_step_status(
            tenant_id, step_id, "waiting", expected_from=("pending", "running")
        )
        task_store.set_task_status(
            tenant_id, task_id, "waiting_owner", expected_from=("running",)
        )
    elif kind is ManagerDecisionKind.ESCALATE:
        task_store.set_step_status(
            tenant_id, step_id, "failed", expected_from=("pending", "running", "waiting")
        )
        task_store.set_task_status(
            tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL)
        )


__all__ = ["ManagerDecision", "ManagerDecisionKind", "decide_next_action", "record_decision"]
