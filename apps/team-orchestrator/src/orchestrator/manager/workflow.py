"""VT-606 (Loop Package 3) — the durable ``manager_task_workflow``.

DBOS workflow keyed by ``(tenant_id, task_id)``:

    load task and plan
        v
    claim current step               (plan_store.claim_next_step — CAS)
        v
    validate capability, prerequisites and policy
        v
    dispatch specialist or advisory tool   (ONE graph.invoke; manager_review runs INSIDE it)
        v
    consume structured result + Manager review decision
        |- accept_step/continue  -> loop: claim next step
        |- complete              -> stop (task 'verifying'; a later verification pass owns the
        |                           final owner-facing outcome — out of VT-606 scope, Package 3
        |                           names it but does not spec its mechanics)
        |- revise_step            -> loop: re-claim the SAME step (now 'pending' again) — up to
        |                           LIMIT_MAX_REVISIONS_PER_STEP times, then blocked+incident
        |- ask_owner               -> durable wait (amendment A3: >24h since the owner's last
        |                           inbound re-engages via the approved template first)
        `- escalate                -> stop (already 'blocked' + a VTR incident — manager_review
                                    raised it)

Mirrors ``runner.py``'s step/workflow discipline: every durable checkpoint is its own
``@DBOS.step()``; the ``@DBOS.workflow()`` body is the plain-Python control flow around them (DBOS
replays the WORKFLOW's code path on recovery, memoizing each step's result — so local Python state
like the revision/cycle counters below is replay-safe AS LONG AS all non-determinism lives inside a
step, which it does here).

Directly callable + independently testable regardless of the global ``TEAM_MANAGER_LOOP_MODE`` —
this module never reads that flag itself; a caller decides WHETHER to start this workflow at all
(``enforce`` mode only, in production — see ``supervisor.build_supervisor_graph``'s own mode gate
for the graph SHAPE this workflow drives).

SCOPE NOTE (found while testing, worth flagging loudly): NEITHER of this workflow's own reachable
outcomes — 'complete' (-> 'verifying') nor 'escalate'/limit-exceeded (-> 'blocked') — lands the task
on a TRUE ``task_store.TASK_TERMINAL`` status ({'completed','failed','cancelled','dead_letter'}).
Both 'verifying' and 'blocked' are, by VT-605's own definition, ``TASK_NON_TERMINAL`` — correctly so:
a verifying task still needs its outcome checked before the owner is told anything, and a blocked
task needs an operator to resolve the incident, so BOTH correctly still occupy the tenant's
one-active-task admission slot (a queued sibling must NOT be promoted just because the active task
is merely verifying-or-blocked). Queue promotion (``_promote_next_queued`` at the end of this
function) is consequently a NO-OP for every outcome this row itself produces — it is WIRED CORRECTLY
and tested (a task that reaches TRUE terminal status by some later mechanism DOES promote its
sibling — see ``test_workflow.py::test_terminal_task_promotes_oldest_queued``), but the "verification
pass" and "blocked-task resolution" that would ever actually MOVE a task to a true terminal status
are, per Package 3's own text, OUT OF VT-606'S SCOPE (a later row's job).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from orchestrator.db import tenant_connection
from orchestrator.manager import plan_store, queue_promotion, task_store
from orchestrator.manager.message_ids import step_thread_id, step_turn_msg_id
from orchestrator.manager.review import ManagerReviewOutcome
from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit

logger = logging.getLogger("orchestrator.manager.workflow")

# Package 3 limits, verbatim. "Eight steps per plan" is enforced structurally by
# ManagerPlan/PlanStep (manager/plan_models.py, max_length=8) at CREATE/REVISE time — nothing
# further to check here. The other two ARE this workflow's job:
LIMIT_MAX_REVISIONS_PER_STEP = 2
LIMIT_MAX_CYCLES = 6

# The owner-answer wait cadence (ask_owner). Not a hot-path wait — an owner reply is not
# time-critical to the second, unlike an ops-triggered run-control pause (runner.py's
# _RUN_CONTROL_POLL_S). ~7 days at this cadence before giving up and blocking+incident-ing rather
# than waiting silently forever (Package 3: "never silence").
_OWNER_WAIT_POLL_S = 300.0
_OWNER_WAIT_MAX_POLLS = 2016  # 2016 * 300s ≈ 7 days

# manager_task_steps.specialist -> activation_registry key. Only sales_recovery has a REAL
# activation_registry entry today (the program baseline's own finding); integration_agent /
# onboarding_conductor have none — so there is NOTHING to validate against for them (treated as
# "no prereq gate defined for this specialist" -> pass), not a fail-closed block. Building
# activation_registry entries for the other two specialists is a product decision outside VT-606's
# scope (the loop MECHANICS), not something this row invents.
_SPECIALIST_TO_ACTIVATION_KEY: dict[str, str] = {"sales_recovery_agent": "sales_recovery"}

# Same six values manager_review decides between (review.py's own dispatch table) — reused here,
# not redefined, so the two can never silently drift apart.
StepOutcome = ManagerReviewOutcome


# ── @DBOS.step() checkpoints ──────────────────────────────────────────────────


@DBOS.step()
def _claim_step(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    return plan_store.claim_next_step(tenant_id, task_id)


@DBOS.step()
def _validate_step(tenant_id: str, step: dict[str, Any]) -> bool:
    """"validate capability, prerequisites and policy" (Package 3's diagram). Fail-closed on the
    prereq check for a specialist this codebase actually gates (sales_recovery); a step whose
    ``allowed_effect_classes`` names something out of the tenant's policy also fails here — before
    ANY dispatch, not after."""
    specialist = step.get("specialist")
    if specialist is not None:
        activation_key = _SPECIALIST_TO_ACTIVATION_KEY.get(specialist)
        if activation_key is not None:
            from orchestrator.agents.onboarding_gate import is_agent_eligible

            with tenant_connection(tenant_id) as conn:
                if not is_agent_eligible(tenant_id, activation_key, conn=conn):
                    return False

    effect_classes = step.get("allowed_effect_classes") or []
    if effect_classes:
        from orchestrator.agents.business_policy import PolicyActionClass, assert_within_policy

        for cls in effect_classes:
            check = assert_within_policy(tenant_id, PolicyActionClass(cls), {})
            if not check.in_policy:
                return False
    return True


@DBOS.step()
def _dispatch_specialist_step(
    tenant_id: str,
    task_id: str,
    step_id: str,
    attempt: int,
    situation: str,
    desired_outcome: str,
    acceptance_criteria: list[str],
    specialist: str | None,
    has_next_step: bool,
) -> str:
    """ONE graph invocation (enforce mode) for ONE specialist-dispatch attempt. ``manager_review``
    runs INSIDE this same ``graph.invoke`` as a node (supervisor.py) — by the time this returns,
    the step's plan_store effect + tm_audit + (escalate-only) incident are ALREADY persisted.
    Returns the ``manager_review_outcome`` string (a plain str — DBOS-step-result-safe; the raw
    LangChain terminal_state is deliberately NOT returned, mirroring ``runner.pipeline_run``'s own
    convention of a step wrapping ``graph.invoke`` returning a simple, serializable value).

    Amendment A4 — thread_id + EVERY injected message id is scoped to ``(task_id, step_id,
    attempt)``: a revise_step re-dispatch increments ``attempt`` (see the workflow loop below), so
    it ALWAYS gets a fresh thread — never reused across attempts (the VT-602 class). A DBOS retry
    of THIS SAME step (a mid-dispatch crash before this step's result committed) re-enters with the
    SAME attempt number, so the stable per-slot ids replace themselves in place at the checkpoint
    instead of appending a duplicate (see manager/message_ids.py).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from orchestrator.agent.dispatch import _BRAIN_MODEL_OPUS, _resolve_model
    from orchestrator.graph import get_checkpointer
    from orchestrator.observability.decorators import observability_context
    from orchestrator.supervisor import build_supervisor_graph

    thread_id = step_thread_id(task_id, step_id, attempt)
    specialist_hint = f" (targets the {specialist} specialist)" if specialist else ""
    messages = [
        SystemMessage(
            content=(
                "## Durable plan step\n"
                f"Situation: {situation}\n"
                f"Desired outcome: {desired_outcome}{specialist_hint}"
            ),
            id=step_turn_msg_id(task_id, step_id, attempt, "situation_block"),
        ),
        HumanMessage(
            content=desired_outcome or situation,
            id=step_turn_msg_id(task_id, step_id, attempt, "human_input"),
        ),
    ]
    initial_state: dict[str, Any] = {
        "messages": messages,
        "tenant_id": UUID(tenant_id),
        "run_id": UUID(task_id),
        "manager_task_id": UUID(task_id),
        "manager_step_id": UUID(step_id),
        "manager_step_situation": situation,
        "manager_step_desired_outcome": desired_outcome,
        "manager_step_acceptance_criteria": acceptance_criteria,
        "manager_has_next_step": has_next_step,
    }
    with observability_context(run_id=UUID(task_id), tenant_id=UUID(tenant_id)):
        graph = build_supervisor_graph(
            model=_resolve_model(_BRAIN_MODEL_OPUS),
            checkpointer=get_checkpointer(),
            mode="enforce",
        )
        terminal_state: dict[str, Any] = graph.invoke(
            initial_state, config={"configurable": {"thread_id": thread_id}}
        )
    return str(terminal_state.get("manager_review_outcome") or "escalate")


@DBOS.step()
def _get_task(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    return task_store.get_task(tenant_id, task_id)


@DBOS.step()
def _has_other_pending_steps(tenant_id: str, task_id: str, plan_revision: int, exclude_step_id: str) -> bool:
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM manager_task_steps WHERE tenant_id = %s AND task_id = %s "
            "AND plan_revision = %s AND status = 'pending' AND id <> %s LIMIT 1",
            (str(tenant_id), str(task_id), plan_revision, str(exclude_step_id)),
        ).fetchone()
    return row is not None


@DBOS.step()
def _block_limit_exceeded(tenant_id: str, task_id: str, *, reason: str) -> None:
    """Package 3: "Limit exhaustion produces blocked plus a VTR incident, never silence." Mirrors
    ``manager.review.manager_review``'s own escalate-incident shape (task_id as the soft
    run_id correlation key)."""
    task_store.set_task_status(
        tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL)
    )
    iid = create_incident(
        tenant_id,
        incident_kind="other",
        run_id=task_id,
        severity="warning",
        detail={"source": "manager_task_workflow", "task_id": str(task_id), "reason": reason},
    )
    if iid is not None:
        escalate_incident(tenant_id, iid, to_tier=2)
    emit_tm_audit(
        event_layer="does",
        event_kind="manager_task_limit_exceeded",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} blocked: {reason}",
        decision={"task_id": str(task_id), "reason": reason},
    )


@DBOS.step()
def _maybe_reengage_stale(tenant_id: str, task_id: str) -> bool:
    """Amendment A3 — if >24h since the owner's last inbound, send the approved re-engagement
    template before continuing to wait for their answer. Returns True iff a re-engage send was
    attempted (success or reported failure — the caller uses this only to avoid re-sending every
    poll tick within the SAME stale window)."""
    from orchestrator.manager.stale_resume import (
        is_stale,
        last_owner_inbound_at,
        reengage_stale_task,
    )

    last_at = last_owner_inbound_at(tenant_id)
    if not is_stale(last_at):
        return False

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT owner_phone FROM tenants WHERE id = %s", (str(tenant_id),)
        ).fetchone()
    owner_phone = (row["owner_phone"] if isinstance(row, dict) else row[0]) if row else None
    if not owner_phone:
        logger.warning(
            "manager_task_workflow: stale-resume skipped — no owner_phone on tenant=%s task=%s",
            tenant_id, task_id,
        )
        return False

    reengage_stale_task(tenant_id, task_id, owner_phone=owner_phone, owner_name="")
    return True


@DBOS.step()
def _question_still_open(tenant_id: str, task_id: str) -> bool:
    from orchestrator.manager import pending_questions

    return bool(pending_questions.get_open(tenant_id, task_id=task_id))


@DBOS.step()
def _resume_step_after_answer(tenant_id: str, task_id: str, step_id: str) -> None:
    """The answered-question counterpart to manager_review's ``ask_owner`` effect: that decision
    parked the step at ``'waiting'`` and the task at ``'waiting_owner'`` —
    ``plan_store.claim_next_step`` only ever claims ``'pending'`` steps, so WITHOUT this transition
    the answered step would sit forever un-reclaimable. CAS-guarded (mirrors task_store's own
    discipline); a stale/already-transitioned state is a no-op, never raised."""
    task_store.set_step_status(tenant_id, step_id, "pending", expected_from=("waiting",))
    task_store.set_task_status(tenant_id, task_id, "running", expected_from=("waiting_owner",))


@DBOS.step()
def _promote_next_queued(tenant_id: str) -> UUID | None:
    return queue_promotion.promote_next_queued_task(tenant_id)


# ── the workflow itself ───────────────────────────────────────────────────────


@DBOS.workflow()
def manager_task_workflow(tenant_id: str, task_id: str) -> str:
    """The durable loop. Returns the task's final status string.

    Local variables (``cycles`` / ``revision_counts``) are replay-safe: DBOS re-executes this
    function's code path on recovery, and every ``@DBOS.step()`` call along that path returns its
    MEMOIZED (already-committed) result rather than re-running — so a replay deterministically
    rebuilds the same counter values by re-walking the same steps, never by re-deriving
    non-deterministic state itself.
    """
    cycles = 0
    revision_counts: dict[str, int] = {}
    attempt_counts: dict[str, int] = {}

    while cycles < LIMIT_MAX_CYCLES:
        step = _claim_step(tenant_id, task_id)
        if step is None:
            break  # nothing pending — plan exhausted, or the task moved to a terminal/waiting state

        step_id = str(step["step_id"])
        if not _validate_step(tenant_id, step):
            _block_limit_exceeded(tenant_id, task_id, reason=f"prereq_or_policy_failed:{step_id}")
            break

        attempt_counts[step_id] = attempt_counts.get(step_id, 0) + 1
        attempt = attempt_counts[step_id]

        task_row = _get_task(tenant_id, task_id)
        plan_revision = int(task_row["plan_revision"]) if task_row else 1
        has_next = _has_other_pending_steps(tenant_id, task_id, plan_revision, step_id)

        outcome = _dispatch_specialist_step(
            tenant_id, task_id, step_id, attempt,
            step.get("situation") or "",
            step.get("desired_outcome") or "",
            step.get("acceptance_criteria") or [],
            step.get("specialist"),
            has_next,
        )
        cycles += 1

        if outcome == "revise_step":
            revision_counts[step_id] = revision_counts.get(step_id, 0) + 1
            if revision_counts[step_id] > LIMIT_MAX_REVISIONS_PER_STEP:
                _block_limit_exceeded(
                    tenant_id, task_id, reason=f"max_revisions_exceeded:{step_id}"
                )
                break
            continue  # re-claim the SAME step (manager_review reset it to 'pending')

        if outcome == "ask_owner":
            reengaged = False
            polls = 0
            while polls < _OWNER_WAIT_MAX_POLLS:
                if not _question_still_open(tenant_id, task_id):
                    break  # answered — correlate_reply already flipped it; resume the loop
                if not reengaged:
                    reengaged = _maybe_reengage_stale(tenant_id, task_id)
                DBOS.sleep(_OWNER_WAIT_POLL_S)
                polls += 1
            else:
                _block_limit_exceeded(tenant_id, task_id, reason="owner_answer_timeout")
                break
            # Answered: the step is still parked at 'waiting' (manager_review's ask_owner effect) —
            # claim_next_step only ever claims 'pending' steps, so transition it back before
            # looping, or the answered step would sit forever un-reclaimable.
            _resume_step_after_answer(tenant_id, task_id, step_id)
            continue  # loop back to claim the (now resumable) step

        if outcome in ("complete", "escalate"):
            break  # manager_review already settled the task terminal (verifying / blocked)

        # outcome in {"continue", "accept_step"} — loop to claim the next step.

    else:
        _block_limit_exceeded(tenant_id, task_id, reason="max_cycles_exceeded")

    final = _get_task(tenant_id, task_id)
    final_status = str(final["status"]) if final else "unknown"
    if final_status in task_store.TASK_TERMINAL:
        _promote_next_queued(tenant_id)
    return final_status


def start_manager_task_workflow(tenant_id: UUID | str, task_id: UUID | str) -> None:
    """Durably START the loop for ``task_id`` — fire-and-forget (``DBOS.start_workflow``), keyed
    by ``(tenant_id, task_id)`` so a REPEAT start for the SAME task (e.g. an idempotent triage
    re-classification) is a safe no-op while the workflow is in flight or already completed. NOT
    the resume path for an ``ask_owner`` pause — that resume happens INSIDE the SAME workflow
    execution via its own durable poll/sleep loop, never a second start."""
    workflow_id = f"manager_task:{tenant_id}:{task_id}"
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(manager_task_workflow, str(tenant_id), str(task_id))


__all__ = [
    "LIMIT_MAX_CYCLES",
    "LIMIT_MAX_REVISIONS_PER_STEP",
    "StepOutcome",
    "manager_task_workflow",
    "start_manager_task_workflow",
]
