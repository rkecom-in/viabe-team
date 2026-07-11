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
        |- complete              -> VERIFY OBJECTIVE (opus checkpoint, team-lead ruling round 2):
        |                           verified -> task 'completed' (a TRUE terminal status —
        |                           terminal_outcome + owner_notification_status='pending' +
        |                           queue promotion fires), then _notify_owner_of_terminal (VT-611
        |                           pre-work #1: the owner-notification composer — closes the
        |                           "truthful owner outcome" gap; fail-soft, never unwinds the
        |                           settle); not_verified -> one revise cycle (appends a step
        |                           addressing the gap) if the verification budget allows, else
        |                           blocked+incident
        |- revise_step            -> loop: re-claim the SAME step (now 'pending' again) — up to
        |                           LIMIT_MAX_REVISIONS_PER_STEP times, then blocked+incident
        |- ask_owner               -> durable wait (amendment A3: >24h since the owner's last
        |                           inbound re-engages via the approved template first); once
        |                           answered, the reply is THREADED into the very next redispatch
        |                           of the same step (VT-611 pre-work #6 —
        |                           _augment_situation_with_answer — the resumed specialist sees
        |                           the owner's answer instead of re-asking the same question)
        |- paused_approval (VT-607, Loop Package 6) -> durable wait for the approval gate's
        |                           SEPARATE resume path (approval_resume.resume_run, driven by
        |                           the webhook path when the owner replies) to resolve
        |                           pending_approvals, THEN ROUTE ON THE OWNER'S ACTUAL DECISION
        |                           (VT-607 fix round — "no longer pending" is NOT "approved"):
        |                             approved      -> VT-633 F-2: EXECUTE the approved
        |                                              campaign_send (_execute_approved_campaign —
        |                                              the loop's OWN execution owner now that F-1
        |                                              stops the legacy graph-resume path from
        |                                              touching a loop-armed approval) + report the
        |                                              REAL sent/skipped/failed counts to the owner,
        |                                              THEN re-enter the SAME verify-then-settle
        |                                              handling 'complete' uses, if that was
        |                                              manager_review's ALREADY-APPLIED decision
        |                             rejected/defer -> task 'cancelled' (a TRUE terminal status),
        |                                              terminal_outcome='cancelled' (the owner
        |                                              notification must read a DECLINE, not a
        |                                              success) + _notify_owner_of_terminal, step
        |                                              stays 'done'
        |                             needs_changes  -> the revise_step path (supersede + a fresh
        |                                              pending replacement), same per-step
        |                                              revision budget; exhausted -> blocked+incident
        |                             timeout        -> blocked+incident (owner_unreachable) —
        |                                              never silence, never auto-success
        |                           never a manager_review outcome itself — a workflow-loop-only
        |                           signal for "the graph paused mid-invoke"
        `- escalate                -> stop (already 'blocked' + a VTR incident — manager_review
                                    raised it; 'blocked' stays non-terminal by design — an operator
                                    resolves the incident, a mechanism this row doesn't build)

Mirrors ``runner.py``'s step/workflow discipline: every durable checkpoint is its own
``@DBOS.step()``; the ``@DBOS.workflow()`` body is the plain-Python control flow around them (DBOS
replays the WORKFLOW's code path on recovery, memoizing each step's result — so local Python state
like the revision/cycle counters below is replay-safe AS LONG AS all non-determinism lives inside a
step, which it does here).

Directly callable + independently testable regardless of the global ``TEAM_MANAGER_LOOP_MODE`` —
this module never reads that flag itself; a caller decides WHETHER to start this workflow at all
(``enforce`` mode only, in production — see ``supervisor.build_supervisor_graph``'s own mode gate
for the graph SHAPE this workflow drives).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from orchestrator.db import tenant_connection
from orchestrator.manager import plan_store, queue_promotion, task_store
from orchestrator.manager.message_ids import loop_run_id, step_turn_msg_id
from orchestrator.manager.review import ManagerReviewOutcome
from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit

logger = logging.getLogger("orchestrator.manager.workflow")

# Package 3 limits, verbatim. "Eight steps per plan" is enforced structurally by
# ManagerPlan/PlanStep (manager/plan_models.py, max_length=8) at CREATE/REVISE time — nothing
# further to check here. The other two ARE this workflow's job:
LIMIT_MAX_REVISIONS_PER_STEP = 2
LIMIT_MAX_CYCLES = 6

# Team-lead ruling round 2: "not_verified -> one revise cycle if the revision budget allows, else
# blocked+incident" — exactly one retry of the completion-verification checkpoint per task.
LIMIT_MAX_VERIFICATION_ATTEMPTS = 1

# The owner-answer wait cadence (ask_owner + the approval wait). Not a hot-path wait to the
# SECOND — but a flat 300s cadence made the manager react to an owner's "haan bhej do" up to
# 5 MINUTES late (VT-633 live canary: approval resolved at :47, first poll at ~:05:43 — the
# owner's yes met dead silence, and the approved send sat unexecuted the whole window). Owners
# answer quickly or not for hours, so the cadence is a deterministic BACKOFF LADDER: the first
# ~10 minutes at 15s (fast reaction while a reply is likely), then 300s (the original DB-churn
# economy for the long tail). The interval is a pure function of the poll INDEX — replay-safe
# (a DBOS re-walk re-derives the identical sleep sequence). Total budget still ≈ 7 days before
# giving up and blocking+incident-ing rather than waiting silently forever (Package 3).
_OWNER_WAIT_POLL_FAST_S = 15.0
_OWNER_WAIT_FAST_POLLS = 40  # 40 × 15s = the first ~10 minutes
_OWNER_WAIT_POLL_S = 300.0
_OWNER_WAIT_MAX_POLLS = 2016 + 40  # 40×15s + 2016×300s ≈ 7 days


def _owner_wait_interval_s(poll_index: int) -> float:
    """VT-633 — the owner-wait backoff ladder (see the constants' note). Pure function of the
    loop index, no live reads: deterministic under DBOS replay by construction."""
    return _OWNER_WAIT_POLL_FAST_S if poll_index < _OWNER_WAIT_FAST_POLLS else _OWNER_WAIT_POLL_S

# manager_task_steps.specialist -> activation_registry key. Only sales_recovery has a REAL
# activation_registry entry today (the program baseline's own finding); onboarding_conductor still
# has none — so there is NOTHING to validate against for it (treated as "no prereq gate defined for
# this specialist" -> pass), not a fail-closed block. VT-608 adds integration_agent's own mapping
# (the VT-606 review flagged its absence — a loop-dispatched integration_agent step previously
# skipped the activation check entirely for the same reason); see activation_registry.REGISTRY's
# integration_agent entry for the declared prerequisites. onboarding_conductor's own entry remains a
# product decision outside this row's scope.
_SPECIALIST_TO_ACTIVATION_KEY: dict[str, str] = {
    "sales_recovery_agent": "sales_recovery",
    "integration_agent": "integration_agent",
}

# The business_impact_choke.BusinessImpactClass values — customer_send is EXCLUDED (VT-460's own
# separate harness, not business_impact_choke's).
_BUSINESS_IMPACT_CLASSES = frozenset({"spend", "commitment", "config"})

# Same six values manager_review decides between (review.py's own dispatch table) — reused here,
# not redefined, so the two can never silently drift apart.
StepOutcome = ManagerReviewOutcome


# ── @DBOS.step() checkpoints ──────────────────────────────────────────────────


@DBOS.step()
def _claim_step(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    return plan_store.claim_next_step(tenant_id, task_id)


@DBOS.step()
def _validate_step(tenant_id: str, step: dict[str, Any]) -> bool:
    """"validate capability, prerequisites and policy" (Package 3's diagram) — the LIVE rails the
    advisory tools already ride, not the dead ``capability/registry.py`` (VT-528) scaffolding.
    ``capability/registry.py`` is a genuinely separate, more elaborate capability-contract system
    (mode/effect-class/verifier/rollback declarations) with no live caller today; wiring THIS diff
    to it would be scope creep in the program's most consequential change (team-lead ruling). It
    stays on the roster as a future consolidation target — a later row should decide whether
    ``_validate_step`` migrates onto it once it has a real caller elsewhere.

    Three checks, fail-closed, all BEFORE any dispatch:
      1. ``onboarding_gate.is_agent_eligible`` — the specialist's activation prerequisites
         (sales_recovery only has a real registry entry today; see the mapping below).
      2. ``business_policy.assert_within_policy`` — the OUTER policy bound for every declared
         effect class (customer_send/spend/commitment/config uniformly).
      3. ``business_impact_choke.assert_or_gate_business_action`` for the three business-impact
         classes (spend/commitment/config; customer_send is VT-460's separate harness). No REAL
         magnitude is known pre-dispatch (a spend/commitment amount is only decided when the
         specialist's OWN gated tool proposes one, e.g. ``marketing_lane.check_ad_spend_intent`` —
         unaffected by VT-606), so this mirrors ``tech_lane.check_config_change_intent``'s own
         magnitude-less convention (``magnitude_minor=0``) — it does NOT replace the specialist's
         real-magnitude gate call at effect-proposal time. A ``requires_owner_approval`` result at
         magnitude 0 is the EXPECTED default for a no-grant/threshold tenant (not itself a block —
         the real gate re-runs with the actual magnitude later); only a ``frozen`` class (an
         explicit owner kill-switch) blocks the step outright — dispatching a specialist to work
         toward an action the owner has explicitly frozen would waste the cycle.
    """
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
        from orchestrator.agents.business_impact_choke import (
            REASON_FROZEN,
            BusinessImpactClass,
            assert_or_gate_business_action,
        )
        from orchestrator.agents.business_policy import PolicyActionClass, assert_within_policy

        for cls in effect_classes:
            check = assert_within_policy(tenant_id, PolicyActionClass(cls), {})
            if not check.in_policy:
                return False
            if cls in _BUSINESS_IMPACT_CLASSES:
                gate = assert_or_gate_business_action(
                    tenant_id, BusinessImpactClass(cls), 0, action_attrs={}
                )
                if gate.reason == REASON_FROZEN:
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
) -> tuple[str, str | None]:
    """ONE graph invocation (enforce mode) for ONE specialist-dispatch attempt. ``manager_review``
    runs INSIDE this same ``graph.invoke`` as a node (supervisor.py) — by the time this returns,
    the step's plan_store effect + tm_audit + (escalate-only) incident are ALREADY persisted.
    Returns ``(manager_review_outcome, manager_review_revised_outcome)`` — a plain
    ``(str, str | None)`` tuple, DBOS-step-result-safe; the raw LangChain terminal_state is
    deliberately NOT returned, mirroring ``runner.pipeline_run``'s own convention of a step wrapping
    ``graph.invoke`` returning a simple, serializable value. The revised-outcome half is round-3's
    MAJOR #4 fix — the reframed desired_outcome to actually apply on a revise_step decision (the
    outer loop's revise_step branch calls ``plan_store.replace_step`` with it), never silently
    discarded.

    Amendment A4 — thread_id + EVERY injected message id is scoped to ``(task_id, step_id,
    attempt)``: a revise_step re-dispatch increments ``attempt`` (see the workflow loop below), so
    it ALWAYS gets a fresh thread — never reused across attempts (the VT-602 class). A DBOS retry
    of THIS SAME step (a mid-dispatch crash before this step's result committed) re-enters with the
    SAME attempt number, so the stable per-slot ids replace themselves in place at the checkpoint
    instead of appending a duplicate (see manager/message_ids.py).

    VT-606 round-3 CRITICAL fix (adversarial review) — the approval-resume invariant: state's
    ``run_id`` MUST equal the graph's checkpoint ``thread_id`` exactly, because
    ``request_owner_approval_node`` persists ``state['run_id']`` into ``pending_approvals`` and
    ``approval_resume.resume_run`` resumes with ``thread_id=str(run_id)`` read back out of that
    row. Both now derive from the SAME ``message_ids.loop_run_id`` value — never ``UUID(task_id)``
    for ``run_id`` while the thread uses a different string (that mismatch orphaned any approval
    interrupt raised through this loop forever, since the resume would target a thread that was
    never checkpointed). ``manager_task_id`` (a SEPARATE, stable field) still carries the actual
    task id for the loop's own correlation — unaffected by this fix.

    VT-607 fix (deferred from VT-606 round-3, blocking) — ``run_id``/``loop_run_id`` is now a REAL
    ``pipeline_runs`` row, minted here before ``graph.invoke``. Both ``pending_approvals.run_id``
    AND ``campaigns.run_id`` carry a foreign key to ``pipeline_runs.id`` (migrations 005/016) — the
    round-3 tests worked around the gap by manually seeding a row; this closes it at the source, so
    ANY loop dispatch that reaches the approval gate or collapse satisfies the FK itself, with no
    test-side seeding needed. Mirrors ``runner.open_webhook_run``/``close_webhook_run``'s own
    columns + status lifecycle (running -> paused|completed|escalated) so the reaper/ops views that
    read ``pipeline_runs`` see loop dispatches the same way they see webhook-driven ones —
    observability parity is the point here, not just satisfying the constraint.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from orchestrator.agent.dispatch import _BRAIN_MODEL_SONNET, _resolve_model
    from orchestrator.graph import get_checkpointer
    from orchestrator.observability.decorators import observability_context
    from orchestrator.supervisor import build_supervisor_graph

    run_id = loop_run_id(task_id, step_id, attempt)
    thread_id = str(run_id)
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
        "run_id": run_id,
        "manager_task_id": UUID(task_id),
        "manager_step_id": UUID(step_id),
        "manager_step_situation": situation,
        "manager_step_desired_outcome": desired_outcome,
        "manager_step_acceptance_criteria": acceptance_criteria,
        "manager_has_next_step": has_next_step,
    }
    _open_dispatch_run(tenant_id, run_id)
    with observability_context(run_id=UUID(task_id), tenant_id=UUID(tenant_id)):
        graph = build_supervisor_graph(
            model=_resolve_model(_BRAIN_MODEL_SONNET),
            checkpointer=get_checkpointer(),
            mode="enforce",
        )
        terminal_state: dict[str, Any] = graph.invoke(
            initial_state, config={"configurable": {"thread_id": thread_id}}
        )

    revised_outcome = terminal_state.get("manager_review_revised_outcome")
    is_paused = "__interrupt__" in terminal_state
    # VT-607 (Loop Package 6) — the outer-loop interrupt-composition fix: a pending interrupt (the
    # approval gate paused this invocation, e.g. Sales Recovery's proposed campaign) is NOT a
    # manager_review outcome at all — LangGraph's interrupted-invoke return does not carry
    # manager_review_outcome (that node's own state update, applied BEFORE the pause, is not part
    # of the narrower interrupt-return payload). Reporting the OLD fallback ("escalate") here would
    # make the outer loop's `if outcome == "escalate": break` treat a live, healthy pause — the
    # owner hasn't even answered yet — as manager_review having already blocked the task with an
    # incident, which is FALSE: manager_review's decision (whatever it was) already applied its
    # plan_store effect before collapse/the gate ever ran. "paused_approval" is a distinct,
    # workflow-loop-only signal (never a manager_review outcome — ManagerReviewOutcome's own type
    # deliberately does not include it) so the loop can durably wait for the SEPARATE resume path
    # (approval_resume.resume_run, driven by the webhook path when the owner replies) to resolve,
    # then continue from wherever the ALREADY-APPLIED decision left the task.
    outcome = "paused_approval" if is_paused else str(terminal_state.get("manager_review_outcome") or "escalate")
    # A pending interrupt leaves the run 'paused' — NOT 'completed' — exactly like
    # close_webhook_run_paused's own convention (mig 052): the run genuinely has not finished, a
    # later resume_run drains it. Everything else is a real terminal of THIS graph invocation;
    # 'escalate' maps to the CHECK constraint's own distinct 'escalated' value (nothing else does —
    # the manager's other five outcomes are all "this invocation ended cleanly", not incident-
    # worthy at the pipeline_runs level).
    if is_paused:
        _close_dispatch_run(tenant_id, run_id, "paused")
    elif outcome == "escalate":
        _close_dispatch_run(tenant_id, run_id, "escalated")
    else:
        _close_dispatch_run(tenant_id, run_id, "completed")

    # VT-608 RULING 3 — the enforce-mode twin of runner.py's post-dispatch ingestion-commit
    # executor. integration_agent's own commit_ingestion TOOL never writes the customer/ledger
    # substrate (VT-268) — it only proposes (tenant_integration_state phase='ingestion_commit_
    # pending'). This is the deterministic, non-agent code path that performs the actual write.
    #
    # VT-608 fix round MAJOR 2 — moved to AFTER outcome/is_paused are resolved (it used to run
    # unconditionally right after graph.invoke, BEFORE manager_review's decision was even read —
    # a revise_step/escalate/paused_approval outcome could not veto a write that had already
    # happened). Now gated on manager_review having ACCEPTED the step: only 'continue' (accept +
    # more steps left) or 'complete' (accept + objective done) fire the executor; 'revise_step'
    # (the specialist's claim wasn't accepted as-is), 'ask_owner'/'escalate' (no acceptance at
    # all), and 'paused_approval' (nothing decided yet — the interrupt hasn't even resolved) never
    # do. Both this hook and runner.py's call the SAME function against the SAME
    # tenant_integration_state truth — no dual-writer race (RULING 1). Fail-soft: an executor
    # failure must never fail this DBOS step; the phase stays observably 'ingestion_commit_pending'
    # rather than a fabricated success.
    if specialist == "integration_agent" and outcome in ("continue", "complete"):
        try:
            from orchestrator.integrations.commit import execute_pending_ingestion_commit

            # MAJOR 1 — task_id is the SAME value this dispatch's own observability_context set
            # as ctx.run_id (see the with-block above), so it matches whatever commit_ingestion
            # armed the proposal with THIS turn.
            execute_pending_ingestion_commit(tenant_id, current_turn_id=task_id)
        except Exception:  # noqa: BLE001 — never fail the dispatch step over a commit-executor bug
            logger.exception(
                "VT-608: execute_pending_ingestion_commit failed tenant=%s task=%s step=%s",
                tenant_id, task_id, step_id,
            )

    return outcome, (str(revised_outcome) if revised_outcome is not None else None)


def _open_dispatch_run(tenant_id: str, run_id: UUID) -> None:
    """Mint the ``pipeline_runs`` row this dispatch's ``run_id`` needs to satisfy the
    ``pending_approvals``/``campaigns`` foreign keys — mirrors ``runner.open_webhook_run``'s own
    INSERT shape (``ON CONFLICT (id) DO NOTHING``: idempotent under a DBOS retry of this same
    step). Not its own ``@DBOS.step()`` — it brackets ``graph.invoke`` inside the SAME step
    boundary as the rest of ``_dispatch_specialist_step``'s body, so a crash mid-dispatch replays
    the whole thing atomically (open -> invoke -> close), never a dangling open with no close."""
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'manager_dispatch', 'running') "
            "ON CONFLICT (id) DO NOTHING",
            (str(run_id), str(tenant_id)),
        )


def _close_dispatch_run(tenant_id: str, run_id: UUID, status: str) -> None:
    """Close this dispatch's ``pipeline_runs`` row — mirrors ``runner.close_webhook_run``'s own
    UPDATE shape. Idempotent (a re-run after a DBOS retry just re-applies the same terminal
    status)."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = now() WHERE id = %s",
            (status, str(run_id)),
        )


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
def _block_limit_exceeded(tenant_id: str, task_id: str, *, limit: str, count: int, threshold: int) -> None:
    """Package 3: "Limit exhaustion produces blocked plus a VTR incident, never silence." Team-lead
    ruling (VT-606 recon follow-up): a self-describing ``limit_exhausted`` incident kind (migration
    166) — never overloaded onto ``other``/``failed_run``, so ops queries can tell "the plan hit a
    budget cap" apart from every other blocked-task cause at a glance. ``detail``/the audit decision
    carry the structured (task_id, limit, count, threshold) shape, not a free-text reason string.

    VT-632 Step 5: 'never silence' now means to the OWNER too, not just the ops incident surface — this
    ARMS terminal_outcome='escalated' + owner_notification_status='pending'; the honest owner notify
    itself fires once at the workflow tail (a SEPARATE memoized step from this arming — mirrors the
    VT-611 settle/notify split, so a notify-replay never re-forces 'pending' and the delivered-flip
    stays a real second backstop against a double-send)."""
    task_store.set_task_status(
        tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL),
        terminal_outcome="escalated", owner_notification_status="pending",
    )
    detail = {"task_id": str(task_id), "limit": limit, "count": count, "threshold": threshold}
    iid = create_incident(
        tenant_id, incident_kind="limit_exhausted", run_id=task_id, severity="warning", detail=detail,
    )
    if iid is not None:
        escalate_incident(tenant_id, iid, to_tier=2)
    emit_tm_audit(
        event_layer="does",
        event_kind="manager_task_limit_exceeded",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} blocked: {limit} exceeded ({count}/{threshold})",
        decision=detail,
    )


@DBOS.step()
def _block_prereq_or_policy_failed(tenant_id: str, task_id: str, *, step_id: str) -> None:
    """A step's capability/prerequisite/policy validation failed BEFORE any dispatch — NOT a limit
    exhaustion (kept off ``limit_exhausted`` so ops can tell the two causes apart); ``other`` + a
    VTR incident, never silence. VT-632 Step 5: also ARMS the honest owner closure (terminal_outcome=
    'escalated' + owner_notification_status='pending'); the notify itself fires at the workflow tail
    (separate memoized step — see _block_limit_exceeded's note)."""
    task_store.set_task_status(
        tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL),
        terminal_outcome="escalated", owner_notification_status="pending",
    )
    detail = {
        "source": "manager_task_workflow", "task_id": str(task_id), "step_id": step_id,
        "reason": "prereq_or_policy_failed",
    }
    iid = create_incident(
        tenant_id, incident_kind="other", run_id=task_id, severity="warning", detail=detail,
    )
    if iid is not None:
        escalate_incident(tenant_id, iid, to_tier=2)
    emit_tm_audit(
        event_layer="does",
        event_kind="manager_task_limit_exceeded",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} blocked: prereq/policy validation failed for step={step_id}",
        decision=detail,
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
def _get_latest_answered_question(tenant_id: str, task_id: str) -> dict[str, Any] | None:
    """VT-611 pre-work #6 — the answer-threading fix's own read. Called right after
    ``_resume_step_after_answer`` so the workflow loop can carry the owner's just-recorded answer
    into the VERY NEXT dispatch of this same step (see ``_augment_situation_with_answer`` +
    its call site in ``manager_task_workflow`` below)."""
    from orchestrator.manager import pending_questions

    return pending_questions.get_latest_answered(tenant_id, task_id)


def _augment_situation_with_answer(situation: str, question_text: str, answer_text: str) -> str:
    """VT-611 pre-work #6 — thread the owner's answer into the resumed specialist's context.

    The bug this closes: ``_dispatch_specialist_step`` builds its messages from ONLY the step's
    ORIGINALLY STORED ``situation``/``desired_outcome`` — before this fix, an ask_owner-resume
    redispatch used that same stale text, with zero mention of the question the owner had just
    answered. The resumed specialist had no way to know its own question had been addressed, so it
    re-asked. Generic (not specialist-specific): every roster specialist's ask_owner flow re-enters
    through this exact loop hop. Pure + deterministic — no DB/network here, the caller already read
    the answer via ``_get_latest_answered_question``."""
    return (
        f"{situation}\n\n"
        f"The owner was asked: {question_text}\n"
        f"The owner answered: {answer_text}"
    )


@DBOS.step()
def _promote_next_queued(tenant_id: str) -> UUID | None:
    return queue_promotion.promote_next_queued_task(tenant_id)


@DBOS.step()
def _verify_completion_step(tenant_id: str, task_id: str) -> tuple[str, str]:
    """Package 3: ``complete -> verify objective``. Amendment A5's OTHER opus checkpoint (the first
    is plan-validation at objective creation — see manager/plan_validation.py). Returns
    ``(verdict, reason)`` — a plain tuple of strings, DBOS-step-result-safe."""
    from orchestrator.manager.verification import verify_completion

    result = verify_completion(tenant_id, task_id)
    return result.verdict, result.reason


@DBOS.step()
def _settle_verified_task(
    tenant_id: str,
    task_id: str,
    *,
    verification_reason: str = "",
    success_closure_not_required: bool = False,
) -> None:
    """A verified completion reaches a TRUE task_store.TASK_TERMINAL status for the first time in
    this row's own outcomes (see the module scope note) — terminal_outcome resolves via the
    evidence-presence proxy PLUS the VT-633 F-3 deterministic executed-effect floor
    (verification.resolve_terminal_outcome), owner_notification_status starts 'pending' (the
    VT-524 seam picks it up from there), and this is what finally makes queue promotion
    (_promote_next_queued, called from the workflow's own tail) a real transition instead of dead
    code.

    ``verification_reason`` (§7D): the opus checkpoint's own ``CompletionVerification.reason`` —
    threaded in by the caller (``_run_verification_cycle``, which already reads it off
    ``_verify_completion_step``) so the audit row records WHY verification passed, not only that
    it did.

    ``success_closure_not_required`` (T12, §2 judge x6 2026-07-11): True when the owner ALREADY
    received the concrete campaign-outcome report this workflow ("Your campaign has gone out —
    sent to N"). The generic success closure would then be a redundant owner-echo ("Done — I've
    taken care of it: <the owner's own message quoted back>") the judge reads as loop_stall —
    so the settle arms owner_notification_status 'not_required' instead of 'pending' and
    _notify_owner_of_terminal cleanly no-ops. A later blocked/escalated terminal still notifies
    (the _block_*/_arm_escalation_for_notify steps re-arm 'pending' themselves)."""
    from orchestrator.manager.verification import resolve_terminal_outcome

    task = task_store.get_task(tenant_id, task_id)
    plan_revision = int(task["plan_revision"]) if task else 1
    steps = [
        s for s in task_store.get_steps(tenant_id, task_id) if s.get("plan_revision") == plan_revision
    ]
    outcome = resolve_terminal_outcome(tenant_id, task_id, steps)
    task_store.set_task_status(
        tenant_id, task_id, "completed", expected_from=("verifying",),
        terminal_outcome=outcome,
        owner_notification_status="not_required" if success_closure_not_required else "pending",
    )
    emit_tm_audit(
        event_layer="does",
        event_kind="task_verified_complete",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} verified complete: terminal_outcome={outcome}",
        decision={
            "task_id": str(task_id),
            "terminal_outcome": outcome,
            "verification_reason": verification_reason,
        },
    )


@DBOS.step()
def _append_verification_retry_step(tenant_id: str, task_id: str, *, reason: str) -> bool:
    """The 'one revise cycle' for a not_verified completion: appends ONE additional step via
    ``plan_store.append_step`` (VT-606 round-3 fix — NOT ``revise_plan``, which re-INSERTS every
    step of a full plan fresh as 'pending'; ``append_step`` carries the existing done history
    forward UNTOUCHED and only inserts the ONE new step, so ``claim_next_step`` claims exactly that
    step instead of re-running the whole plan from step 1), then transitions the task back to
    'running' so the outer loop's claim_next_step picks it up. Returns False (no retry attempted)
    when the plan is already at PlanStep's own 8-step ceiling — the caller treats that as
    budget-exhausted too."""
    from orchestrator.manager.plan_models import PlanStep

    plan = plan_store.load_plan(tenant_id, task_id)
    if plan is None or len(plan.steps) >= 8:
        return False
    next_seq = len(plan.steps) + 1
    retry_step = PlanStep(
        step_seq=next_seq,
        kind="verification",
        situation=f"Completion verification did not pass: {reason}",
        desired_outcome="Address the verification gap and re-confirm the objective is met.",
    )
    plan_store.append_step(
        tenant_id, task_id, retry_step, expected_plan_revision=plan.plan_revision
    )
    task_store.set_task_status(tenant_id, task_id, "running", expected_from=("verifying",))
    # §7D DECIDES — record WHY a retry step was appended (the opus checkpoint's own
    # not_verified reason), not just that one was.
    emit_tm_audit(
        event_layer="decides",
        event_kind="verification_retry_appended",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} verification retry appended: {reason}",
        decision={"task_id": str(task_id), "reason": reason},
    )
    return True


def _run_verification_cycle(
    tenant_id: str,
    task_id: str,
    verification_attempts: int,
    *,
    campaign_outcome_reported: bool = False,
) -> tuple[str, int]:
    """Package 3: "complete -> verify objective" (team-lead ruling round 2) — manager_review
    settled the task 'verifying', but that is NOT yet a true terminal status. Run the opus
    completion-verification checkpoint.

    VT-607 (Loop Package 6): shared by BOTH the 'complete' outcome branch AND the 'paused_approval'
    resolution branch — an ACCEPT decision reached via either path lands the task at 'verifying'
    the identical way (manager_review's OWN plan_store effect ran before the approval gate ever
    paused anything), so both need the SAME verify-then-settle handling; extracted here rather than
    duplicated.

    Returns ``(action, updated_verification_attempts)`` where ``action`` is one of:
      - 'settled' — verified; the caller should stop the loop (a true terminal status now).
      - 'retry'   — a gap-addressing step was appended; the caller should continue (re-claim it).
      - 'blocked' — the verification-attempt budget is exhausted; the caller should stop.
    """
    verdict, reason = _verify_completion_step(tenant_id, task_id)
    if verdict == "verified":
        _settle_verified_task(
            tenant_id, task_id, verification_reason=reason,
            success_closure_not_required=campaign_outcome_reported,
        )
        _notify_owner_of_terminal(tenant_id, task_id)
        return "settled", verification_attempts
    verification_attempts += 1
    if (
        verification_attempts <= LIMIT_MAX_VERIFICATION_ATTEMPTS
        and _append_verification_retry_step(tenant_id, task_id, reason=reason)
    ):
        return "retry", verification_attempts
    _block_limit_exceeded(
        tenant_id, task_id,
        limit="verification_attempts_exceeded",
        count=verification_attempts,
        threshold=LIMIT_MAX_VERIFICATION_ATTEMPTS,
    )
    return "blocked", verification_attempts


@DBOS.step()
def _approval_still_pending(tenant_id: str, task_id: str, step_id: str, attempt: int) -> bool:
    """VT-607 (Loop Package 6) — the 'paused_approval' wait target: is the interrupt's OWN
    pending_approvals row still unresolved? Uses the SAME message_ids.loop_run_id the dispatch
    minted as both its checkpoint thread_id and state['run_id'] (the round-3 CRITICAL fix) — the
    durable link between a paused dispatch and the approval row it raised. ``task_id`` is accepted
    for a consistent step signature (mirrors the other per-task steps) but not itself queried —
    the run_id already uniquely identifies the row.
    """
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    run_id = loop_run_id(task_id, step_id, attempt)
    # Wrapper-layer read (VT-72/306 no-direct-tenant-db-access gate).
    status = PendingApprovalsWrapper().status_for_run(tenant_id, run_id)
    if status is None:
        # No approval row at all for this run_id — defensive; should not happen (the dispatch
        # only reports 'paused_approval' when the graph itself raised the interrupt, which is
        # exactly what arm_pause_request's INSERT is paired with). Nothing to wait for.
        return False
    return status == "pending"


@DBOS.step()
def _approval_decision_for_run(tenant_id: str, task_id: str, step_id: str, attempt: int) -> str | None:
    """VT-607 fix round (CRITICAL) — once ``_approval_still_pending`` reports resolved, read the
    owner's ACTUAL decision verb (``approved`` | ``rejected`` | ``needs_changes`` | ``defer`` |
    ``timeout``) so the loop can route on it. The bug this closes: the loop previously treated
    "no longer pending" as synonymous with "approved" — a REJECTED or timed-out campaign would
    unconditionally run the verification cycle and settle 'completed_with_effect' with a SUCCESS
    notification, discarding the owner's Pillar-7-authoritative 'no' at the loop layer (collapse's
    own campaign_execute path never even runs on a non-approved decision — the settle was simply
    WRONG, not just premature)."""
    from orchestrator.db.wrappers import PendingApprovalsWrapper

    run_id = loop_run_id(task_id, step_id, attempt)
    return PendingApprovalsWrapper().decision_for_run(tenant_id, run_id)


@DBOS.step()
def _execute_approved_campaign(tenant_id: str, task_id: str, step_id: str, attempt: int) -> dict[str, Any]:
    """VT-633 F-2 — the loop's OWN execution owner for an approved ``campaign_send``.

    The defect this closes: once ``_approval_decision_for_run`` reads ``decision == 'approved'``,
    the loop re-entered the verify-then-settle handling and settled the task 'completed_with_effect'
    — but NOTHING had actually sent the campaign. The legacy graph-resume path
    (``runner.try_resume_pending_approval`` -> ``supervisor._campaign_execute_node``) was the only
    real execution owner; F-1 (built separately) stops ``try_resume`` from graph-resuming a
    loop-armed approval (``run_type == 'manager_dispatch'``), so after F-1 this loop is the ONLY
    reactor left — without this step a real customer campaign never fires and the owner hears a
    fabricated "Done — I've taken care of it" off the PROPOSAL step's own evidence_kind (written
    before approval, not after execution).

    Loads the SAME run's approval row ``_approval_decision_for_run`` just read 'approved' off
    (``PendingApprovalsWrapper.approval_for_run`` — VT-72/306: wrapper-layer read, never raw SQL on
    ``pending_approvals``) and, only when it is a resolved ``campaign_send`` approval carrying a
    ``campaign_id``, executes it. Any other shape (a ``sensitive_data_access``/``cohort_size_
    exceeded`` approval, a missing row, a missing campaign_id) is a clean no-op — nothing to send.

    Idempotency (a DBOS retry of this step after a completed execution must NOT double-send): the
    campaign's own ``status`` is read via ``CampaignsWrapper.get_status`` BEFORE calling in — only
    'proposed'/'approved' proceed. ``campaign.execute.execute_approved_campaign`` advances the
    campaign to 'sent' inside its own atomic status+``CAMPAIGN_SENT``-emit transaction (gated on
    ``killed == 0``; see its own module docstring D2 + the VT-558 true-kill guards) — once THAT
    commits, a replayed call to this step reads a non-'proposed'/'approved' status and short-
    circuits here, before ever re-entering the fan-out loop. This step's own guard is what makes
    the retry safe; ``execute_approved_campaign`` is not re-derived or duplicated.

    Mirrors ``supervisor._campaign_execute_node``'s wrapping (the SAME function, the SAME
    ordering): the VT-374 run-control hold check for (tenant, 'campaign_send') runs BEFORE any
    send — if held, this returns without calling in. Never raises: every non-executing path (no-op
    / held / error) returns ``{"executed": False, "reason": ...}``; the caller only reports the
    outcome to the owner when ``executed`` is True.
    """
    from orchestrator.db.wrappers import CampaignsWrapper, PendingApprovalsWrapper

    run_id = loop_run_id(task_id, step_id, attempt)
    approval = PendingApprovalsWrapper().approval_for_run(tenant_id, run_id)
    if approval is None:
        return {"executed": False, "reason": "no_approval_row"}
    if approval.get("decision") != "approved" or approval.get("approval_type") != "campaign_send":
        return {
            "executed": False,
            "reason": f"not_a_campaign_send_approval:{approval.get('approval_type')}",
        }
    campaign_id = approval.get("campaign_id")
    if not campaign_id:
        return {"executed": False, "reason": "no_campaign_id"}

    status = CampaignsWrapper().get_status(tenant_id, campaign_id)
    if status not in ("proposed", "approved"):
        # Idempotency guard (see docstring above): execute_approved_campaign's OWN status-advance
        # to 'sent' (or an ops true-kill to 'cancelled') already ran — a replayed call must not
        # re-enter the fan-out and re-send every recipient.
        return {"executed": False, "reason": f"campaign_not_pending_execution:{status}"}

    from orchestrator.run_control import check_pause

    if check_pause(tenant_id, "campaign_send"):
        logger.info(
            "_execute_approved_campaign: HELD by run-control pause tenant=%s campaign=%s",
            tenant_id, campaign_id,
        )
        return {"executed": False, "reason": "run_control_hold"}

    try:
        from orchestrator.campaign.execute import execute_approved_campaign

        with tenant_connection(tenant_id) as conn:
            summary = execute_approved_campaign(tenant_id, campaign_id, conn=conn)
    except Exception as exc:  # noqa: BLE001 — never raise out of the loop over a send-execution bug
        logger.exception(
            "_execute_approved_campaign: error tenant=%s task=%s campaign=%s",
            tenant_id, task_id, campaign_id,
        )
        return {"executed": False, "reason": f"error:{type(exc).__name__}"}

    return {"executed": True, "summary": summary}


@DBOS.step()
def _report_campaign_outcome_to_owner(tenant_id: str, run_id: UUID, summary: dict[str, Any]) -> bool:
    """VT-633 F-2 — the honest owner-facing counterpart to ``_execute_approved_campaign``: reports
    the REAL sent/skipped/failed counts via ``owner_surface.campaign_outcome`` (the same composer
    the legacy graph-resume path used before F-1's redirect), so the owner hears what actually
    happened instead of the VT-633 fabricated "Done".

    Its OWN memoized step — deliberately NOT an un-stepped call from the workflow body.
    ``maybe_report_campaign_outcome`` has NO internal idempotency guard (unlike
    ``_notify_owner_of_terminal``'s ``maybe_notify_owner_of_task_outcome``, which no-ops once
    ``owner_notification_status`` leaves 'pending'): a DBOS replay hitting an un-stepped call here
    would re-send the WhatsApp outcome report to the owner every time the workflow re-executes past
    this point. Fail-soft by the callee's own contract (never raises); a no-activity/no-phone
    summary is a clean no-op inside it.

    T12 (§2 judge x6, 2026-07-11): RETURNS whether a report was actually dispatched. The caller
    threads that flag into the settle path so the later generic success closure ("Done — I've
    taken care of it: <the owner's own message quoted back>") is suppressed — a redundant
    owner-echo the judge reads as loop_stall once the owner already heard the concrete result.
    The bool is a memoized step result, so the downstream branching on it is replay-deterministic.
    """
    from orchestrator.owner_surface.campaign_outcome import maybe_report_campaign_outcome

    return maybe_report_campaign_outcome(
        tenant_id, {"campaign_execution_summary": summary}, run_id=run_id
    )


@DBOS.step()
def _settle_declined_approval(tenant_id: str, task_id: str) -> None:
    """VT-607 fix round — the owner's REJECTED (or an exhausted-defer, which resolves the same
    way per approval_resume._DECISION_TO_STATUS) decision. The step's own work stayed 'done' (it
    genuinely ran — the owner declined the EFFECT, not the specialist's work); the task settles a
    TRUE terminal ('cancelled', already a task_store.TASK_TERMINAL member) with
    terminal_outcome='cancelled' so whatever composes the owner notification reads a decline, never
    a success. owner_notification_status starts 'pending' — the SAME VT-524 seam that would have
    picked up a verified completion picks this up too."""
    task_store.set_task_status(
        tenant_id, task_id, "cancelled", expected_from=("verifying",),
        terminal_outcome="cancelled", owner_notification_status="pending",
    )
    emit_tm_audit(
        event_layer="does",
        event_kind="task_approval_declined",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} cancelled: owner declined the proposed effect",
        decision={"task_id": str(task_id), "terminal_outcome": "cancelled"},
    )


@DBOS.step()
def _notify_owner_of_terminal(tenant_id: str, task_id: str) -> None:
    """VT-611 pre-work #1 — the owner-notification composer's own DBOS checkpoint. Called right
    after EITHER settle path lands (_settle_verified_task via _run_verification_cycle;
    _settle_declined_approval directly) so a completed/cancelled task actually tells the owner
    something, closing the "truthful owner outcome" gap (terminal_outcome +
    owner_notification_status='pending' were recorded since mig 165, but nothing sent until this).
    VT-632 Step 5 REUSES this SAME step at the workflow tail for a 'blocked'/escalated terminal
    (armed by the _block_* steps / _arm_escalation_for_notify) — deliberately a SEPARATE memoized
    step from the arming so a notify-replay never re-forces owner_notification_status back to
    'pending', keeping the delivered-flip a real second backstop against a double-send.
    Fail-soft by construction (owner_surface.task_outcome never raises) — a notification-send
    failure must never unwind the settle/block that already committed. Idempotent: maybe_notify
    only sends while owner_notification_status=='pending' (a re-call on a delivered/failed/
    not_required row is a clean no-op)."""
    from orchestrator.owner_surface.task_outcome import maybe_notify_owner_of_task_outcome

    maybe_notify_owner_of_task_outcome(tenant_id, task_id)


@DBOS.step()
def _arm_escalation_for_notify(tenant_id: str, task_id: str) -> None:
    """VT-632 Step 5 — the manager_review 'escalate' outcome already settled the task 'blocked' + a
    VTR incident inside manager_review's OWN module, so (unlike the _block_* paths) it never passed
    through a step that ARMED the owner notify. Record terminal_outcome='escalated' +
    owner_notification_status='pending' on the already-'blocked' task (idempotent CAS — 'blocked' is
    a member of TASK_NON_TERMINAL, so the re-set is accepted and simply stamps the two columns).
    ARM-ONLY: the honest owner closure itself fires once at the workflow tail via
    _notify_owner_of_terminal — a separate memoized step from this arming (see that step's note)."""
    task_store.set_task_status(
        tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL),
        terminal_outcome="escalated", owner_notification_status="pending",
    )


@DBOS.step()
def _resume_task_after_needs_changes(tenant_id: str, task_id: str) -> None:
    """VT-607 fix round — a 'needs_changes' decision replaces the step (via _apply_step_revision,
    called by the caller just before this) but manager_review's ORIGINAL 'complete' decision had
    already moved the task to 'verifying' before the pause — claim_next_step's own task-level CAS
    guard only accepts 'planned'/'running' predecessors, so WITHOUT this the outer loop's re-claim
    would silently no-op forever on a task stuck 'verifying' with a freshly-pending replacement
    step nothing would ever claim. Mirrors _resume_step_after_answer's own transition-back pattern
    for the ask_owner branch."""
    task_store.set_task_status(tenant_id, task_id, "running", expected_from=("verifying",))


@DBOS.step()
def _block_owner_unreachable(tenant_id: str, task_id: str) -> None:
    """VT-607 fix round — the approval itself timed out (decision='timeout', the scheduled 48h
    sweep resolved it — NOT this loop's own approval_resolution_timeout poll-exhaustion, a
    DIFFERENT case already handled by _block_limit_exceeded above). 'other' + a VTR incident,
    never silence, never an auto-success settle — an operator needs to re-engage the owner. VT-632
    Step 5: also ARMS the honest owner closure (terminal_outcome='escalated' +
    owner_notification_status='pending'); the notify fires at the workflow tail (separate memoized
    step — see _block_limit_exceeded's note)."""
    task_store.set_task_status(
        tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL),
        terminal_outcome="escalated", owner_notification_status="pending",
    )
    detail = {"task_id": str(task_id), "reason": "owner_unreachable"}
    iid = create_incident(
        tenant_id, incident_kind="other", run_id=task_id, severity="warning", detail=detail,
    )
    if iid is not None:
        escalate_incident(tenant_id, iid, to_tier=2)
    emit_tm_audit(
        event_layer="does",
        event_kind="manager_task_owner_unreachable",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"task={task_id} blocked: approval timed out, owner unreachable",
        decision=detail,
    )


@DBOS.step()
def _apply_step_revision(
    tenant_id: str, task_id: str, step: dict[str, Any], revised_outcome: str | None,
) -> bool:
    """VT-606 round-3 fix, MAJOR #4: a revise_step decision must actually APPLY the reframed
    outcome (``ManagerDecision.revised_outcome``, surfaced via ``manager_review_revised_outcome``)
    — the bug this closes: manager_review reset the step to 'pending' but the revised text was
    never applied anywhere, so the re-dispatch used the STALE original desired_outcome.

    Builds a replacement PlanStep (SAME step_seq/kind/specialist/acceptance_criteria/
    allowed_effect_classes as the original — only desired_outcome changes to the reframed text;
    situation, the underlying business context, is unchanged) and calls
    ``plan_store.replace_step`` (supersedes the OLD step as real history, carries every OTHER
    non-superseded step forward untouched, inserts the replacement fresh as 'pending' — never
    re-runs the whole plan, the SAME class of fix as ``append_step``). The replacement gets a
    BRAND NEW step_id, so its own attempt counter starts fresh (amendment A4: a revised
    step is a genuinely new dispatch context — never reuses the old step's thread/messages).

    Returns False (no revision applied — a defensive no-op, not a crash) when the graph produced
    no revised_outcome text at all (should be structurally unreachable: decide_next_action only
    reaches REVISE via a PUSHBACK carrying ``proposed_outcome`` — guarded anyway since a future
    upstream change could regress it silently).
    """
    if not revised_outcome:
        logger.warning(
            "_apply_step_revision: revise_step outcome with no revised_outcome text for "
            "task=%s step=%s — nothing to apply, treating as a no-op", task_id, step.get("step_id"),
        )
        return False

    from orchestrator.manager.plan_models import PlanStep

    task = task_store.get_task(tenant_id, task_id)
    plan_revision = int(task["plan_revision"]) if task else 1
    replacement = PlanStep(
        step_seq=step["step_seq"],
        kind=step["kind"],
        specialist=step.get("specialist"),
        situation=step.get("situation") or "",
        desired_outcome=revised_outcome,
        acceptance_criteria=step.get("acceptance_criteria") or [],
        allowed_effect_classes=step.get("allowed_effect_classes") or [],
    )
    plan_store.replace_step(
        tenant_id, task_id, step["step_id"], replacement, expected_plan_revision=plan_revision
    )
    return True


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
    # Keyed by step_seq (NOT step_id): a revise_step application (round-3 MAJOR #4) replaces the
    # step on a BRAND NEW step_id every time (the old one is superseded, real history) — step_seq
    # is the stable identity of "the same conceptual step" across that chain of replacements.
    # attempt_counts stays keyed by step_id on purpose: a replacement step is a genuinely fresh
    # dispatch context (amendment A4), so its own attempt count correctly starts at 1.
    revision_counts: dict[int, int] = {}
    attempt_counts: dict[str, int] = {}
    verification_attempts = 0
    # T12 — True once the owner received the concrete campaign-outcome report this workflow
    # (a memoized step result, so replay re-derives the identical value). Threaded into every
    # verification cycle so the settle suppresses the redundant generic success closure.
    campaign_outcome_reported = False
    # VT-611 pre-work #6 (answer-threading): set by the ask_owner branch right after an owner
    # reply resumes a step; consumed EXACTLY ONCE, on the very next dispatch of that SAME step_id,
    # then cleared — never leaks into a later revise_step/ask_owner cycle for the same step.
    # Replay-safe like every other local counter here: DBOS re-derives it deterministically by
    # re-walking the same committed step results.
    pending_answer_step_id: str | None = None
    pending_answer_situation: str | None = None

    while cycles < LIMIT_MAX_CYCLES:
        step = _claim_step(tenant_id, task_id)
        if step is None:
            break  # nothing pending — plan exhausted, or the task moved to a terminal/waiting state

        step_id = str(step["step_id"])
        if not _validate_step(tenant_id, step):
            _block_prereq_or_policy_failed(tenant_id, task_id, step_id=step_id)
            break

        attempt_counts[step_id] = attempt_counts.get(step_id, 0) + 1
        attempt = attempt_counts[step_id]

        task_row = _get_task(tenant_id, task_id)
        plan_revision = int(task_row["plan_revision"]) if task_row else 1
        has_next = _has_other_pending_steps(tenant_id, task_id, plan_revision, step_id)

        situation = step.get("situation") or ""
        if pending_answer_step_id == step_id:
            # The answer-threading fix's consume point: this is the FIRST re-claim of the step an
            # owner reply just resumed — carry the Q&A into THIS dispatch, then never again.
            situation = pending_answer_situation or situation
            pending_answer_step_id = None
            pending_answer_situation = None

        outcome, revised_outcome = _dispatch_specialist_step(
            tenant_id, task_id, step_id, attempt,
            situation,
            step.get("desired_outcome") or "",
            step.get("acceptance_criteria") or [],
            step.get("specialist"),
            has_next,
        )
        cycles += 1

        if outcome == "revise_step":
            step_seq = int(step["step_seq"])
            revision_counts[step_seq] = revision_counts.get(step_seq, 0) + 1
            if revision_counts[step_seq] > LIMIT_MAX_REVISIONS_PER_STEP:
                _block_limit_exceeded(
                    tenant_id, task_id,
                    limit=f"max_revisions_per_step_seq:{step_seq}",
                    count=revision_counts[step_seq],
                    threshold=LIMIT_MAX_REVISIONS_PER_STEP,
                )
                break
            # Apply the reframed outcome (round-3 MAJOR #4) — replaces the OLD step (now
            # superseded, real history) with a NEW one carrying the revised desired_outcome, so
            # the re-claim below actually re-dispatches with the manager's ACTUAL revision rather
            # than the stale original framing.
            _apply_step_revision(tenant_id, task_id, step, revised_outcome)
            continue  # re-claim — the replacement step is now the pending one

        if outcome == "ask_owner":
            reengaged = False
            polls = 0
            while polls < _OWNER_WAIT_MAX_POLLS:
                if not _question_still_open(tenant_id, task_id):
                    break  # answered — correlate_reply already flipped it; resume the loop
                if not reengaged:
                    reengaged = _maybe_reengage_stale(tenant_id, task_id)
                DBOS.sleep(_owner_wait_interval_s(polls))
                polls += 1
            else:
                _block_limit_exceeded(
                    tenant_id, task_id,
                    limit="owner_answer_timeout", count=polls, threshold=_OWNER_WAIT_MAX_POLLS,
                )
                break
            # Answered: the step is still parked at 'waiting' (manager_review's ask_owner effect) —
            # claim_next_step only ever claims 'pending' steps, so transition it back before
            # looping, or the answered step would sit forever un-reclaimable.
            _resume_step_after_answer(tenant_id, task_id, step_id)
            # VT-611 pre-work #6 — thread the owner's answer into the VERY NEXT dispatch of this
            # same step (consumed once, at the top of the loop, then cleared). Without this the
            # resumed specialist redispatches with only the step's ORIGINAL stored situation and
            # has no idea the owner just answered its own question — it re-asks.
            answered = _get_latest_answered_question(tenant_id, task_id)
            if answered is not None:
                pending_answer_step_id = step_id
                pending_answer_situation = _augment_situation_with_answer(
                    step.get("situation") or "",
                    str(answered.get("question_text") or ""),
                    str(answered.get("answer_text") or ""),
                )
            continue  # loop back to claim the (now resumable) step

        if outcome == "escalate":
            # manager_review already settled the task blocked + a VTR incident; VT-632 Step 5 ARMS
            # the honest owner closure here (the notify itself fires at the tail) so an escalate
            # never lands as silence after the interim ack.
            _arm_escalation_for_notify(tenant_id, task_id)
            break

        if outcome == "paused_approval":
            # VT-607 (Loop Package 6) — the interrupt-composition fix: the approval gate paused
            # THIS dispatch (e.g. Sales Recovery's proposed campaign). manager_review's OWN
            # decision already applied its plan_store effect BEFORE collapse/the gate ever ran —
            # this loop does NOT resume the graph itself (that is approval_resume.resume_run's
            # job, driven by a COMPLETELY SEPARATE code path — the webhook ingress, when the
            # owner's WhatsApp reply arrives); it durably waits for THAT resolution the same way
            # the ask_owner branch waits for an answer, then continues from wherever the already-
            # applied decision left the task.
            polls = 0
            resolved = False
            while polls < _OWNER_WAIT_MAX_POLLS:
                if not _approval_still_pending(tenant_id, task_id, step_id, attempt):
                    resolved = True
                    break
                DBOS.sleep(_owner_wait_interval_s(polls))
                polls += 1
            if not resolved:
                _block_limit_exceeded(
                    tenant_id, task_id,
                    limit="approval_resolution_timeout", count=polls, threshold=_OWNER_WAIT_MAX_POLLS,
                )
                break

            # VT-607 fix round (CRITICAL) — route on the owner's ACTUAL decision, never on "no
            # longer pending" alone (that treated ANY resolution — including a REJECTED or timed-
            # out approval — as an implicit approve, settling 'completed_with_effect' with a
            # success notification regardless of what the owner actually said. Pillar-7 forbids
            # that at every OTHER layer; this closes the same gap at the loop layer).
            decision = _approval_decision_for_run(tenant_id, task_id, step_id, attempt)

            if decision == "approved":
                # VT-633 F-2 — the loop's OWN execution owner: unconditional single step call
                # (DBOS replay-determinism — never behind a live conditional read); the campaign
                # send itself is idempotent under a retry (see _execute_approved_campaign's own
                # docstring for the guard). Without this the campaign stayed 'proposed' with 0
                # campaign_messages while the verify-then-settle handling below settled
                # 'completed_with_effect' off the PROPOSAL step's own evidence_kind — the VT-633
                # defect (a false "Done — I've taken care of it").
                exec_result = _execute_approved_campaign(tenant_id, task_id, step_id, attempt)
                # VT-633 — persist the execution outcome to the audit spine UNCONDITIONALLY.
                # Tonight's live debugging went blind twice because the step's no-op/summary
                # reasons lived only in its return value; every approved-branch execution now
                # leaves a durable trace (counts only — no PII).
                emit_tm_audit(
                    event_layer="does", event_kind="campaign_execution_result",
                    actor="team_manager", tenant_id=tenant_id,
                    summary=f"approved-campaign execution: executed={exec_result.get('executed')}",
                    decision={k: v for k, v in exec_result.items() if k in ("executed", "reason", "summary")},
                    # §7D — joins to the ORIGINAL proposing dispatch's orchestrator_agent_turn
                    # reasoning_turn row. Deliberately task_id, NOT loop_run_id(task_id, step_id,
                    # attempt): loop_run_id is the structural correlator (campaigns.run_id,
                    # pending_approvals.run_id, the checkpoint thread_id) — but EVERY reasoning-
                    # capturing write in a loop dispatch (langchain_callback.py's orchestrator turn,
                    # and specialist turns via decorators.with_reasoning_capture) is keyed by the
                    # ObservabilityContext's run_id, which _dispatch_specialist_step sets to
                    # UUID(task_id) regardless of attempt — task_id is what actually resolves here.
                    reasoning_ref={"run_id": str(task_id), "step_name": "orchestrator_agent_turn"},
                )
                if exec_result.get("executed"):
                    # Tell the owner what actually happened, from the REAL summary — its own
                    # memoized step (see _report_campaign_outcome_to_owner's docstring for why an
                    # un-stepped call here would be replay-unsafe). T12: the returned bool feeds
                    # the settle's success-closure suppression (no redundant owner-echo).
                    campaign_outcome_reported = (
                        _report_campaign_outcome_to_owner(
                            tenant_id, loop_run_id(task_id, step_id, attempt),
                            exec_result.get("summary") or {},
                        )
                        or campaign_outcome_reported
                    )
                task_now = _get_task(tenant_id, task_id)
                if task_now is not None and task_now.get("status") == "verifying":
                    # The paused dispatch's decision was 'complete' (ACCEPT) — same verify-then-
                    # settle handling the 'complete' branch below runs, reused not duplicated.
                    action, verification_attempts = _run_verification_cycle(
                        tenant_id, task_id, verification_attempts,
                        campaign_outcome_reported=campaign_outcome_reported,
                    )
                    if action == "retry":
                        continue
                    break  # 'settled' or 'blocked'
                continue  # any other status (e.g. still 'running' from a continue decision)

            if decision in ("rejected", "defer"):
                # 'defer' only reaches here once EXHAUSTED (approval_resume._MAX_DEFERS) — it
                # resolves the same way a rejection does (status='rejected'); the audit truth of
                # WHICH is preserved in pending_approvals.decision, not re-derived here.
                _settle_declined_approval(tenant_id, task_id)
                _notify_owner_of_terminal(tenant_id, task_id)
                break

            if decision == "needs_changes":
                step_seq = int(step["step_seq"])
                revision_counts[step_seq] = revision_counts.get(step_seq, 0) + 1
                if revision_counts[step_seq] > LIMIT_MAX_REVISIONS_PER_STEP:
                    _block_limit_exceeded(
                        tenant_id, task_id,
                        limit=f"max_revisions_per_step_seq:{step_seq}",
                        count=revision_counts[step_seq], threshold=LIMIT_MAX_REVISIONS_PER_STEP,
                    )
                    break
                _apply_step_revision(
                    tenant_id, task_id, step,
                    "The owner requested changes to the proposed campaign before approving — "
                    "reconsider the approach and address their concern before proposing again.",
                )
                _resume_task_after_needs_changes(tenant_id, task_id)
                continue  # re-claim — the replacement step is now the pending one

            # decision == "timeout", or (defensively) an unrecognized/None value reached after a
            # resolution was observed — never silence, never an auto-success settle.
            _block_owner_unreachable(tenant_id, task_id)
            break

        if outcome == "complete":
            action, verification_attempts = _run_verification_cycle(
                tenant_id, task_id, verification_attempts,
                campaign_outcome_reported=campaign_outcome_reported,
            )
            if action == "retry":
                continue  # re-claim the freshly-appended verification-retry step
            break  # 'settled' or 'blocked'

        # outcome in {"continue", "accept_step"} — loop to claim the next step.

    else:
        _block_limit_exceeded(
            tenant_id, task_id, limit="max_cycles", count=cycles, threshold=LIMIT_MAX_CYCLES
        )

    final = _get_task(tenant_id, task_id)
    final_status = str(final["status"]) if final else "unknown"
    if final_status == "blocked":
        # VT-632 Step 5 — every terminal BLOCK (limit / prereq / owner-unreachable / escalate) armed
        # terminal_outcome='escalated' + owner_notification_status='pending' in its OWN memoized step
        # above; fire the honest owner closure HERE, once, at the single tail every block `break`
        # reaches — a SEPARATE memoized step from the arming (mirrors the VT-611 settle/notify split)
        # so a notify-replay never re-forces 'pending' and the delivered-flip stays a real second
        # backstop against a double-send. Idempotent + fail-soft: maybe_notify only sends while
        # owner_notification_status=='pending', so a block that was NOT armed (default 'not_required')
        # is a clean no-op, and a re-entry after a delivered send sends nothing.
        _notify_owner_of_terminal(tenant_id, task_id)
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
