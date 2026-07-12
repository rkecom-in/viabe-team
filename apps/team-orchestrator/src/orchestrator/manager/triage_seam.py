"""VT-606 (team-lead ruling round 2, hardened round 3) — wires ``manager.triage`` into the live
dispatch seam (``runner.webhook_pipeline_run``, immediately before its ``dispatch_brain`` call),
mode-gated at the read site so each mode's behavior is exactly what the team-lead ruling specifies:

  legacy  -> ZERO new calls. The hot path is BYTE-IDENTICAL to pre-VT-606 — ``triage_seam`` returns
             immediately without even IMPORTING ``triage.triage_turn``, let alone calling it. Pinned
             by ``test_triage_seam.py::test_legacy_mode_never_calls_triage``.
  shadow  -> triage runs OBSERVATIONALLY: classify -> (new_task: opus-validate a minimal draft plan
             -> create_plan(shadow=True), keyed on the inbound message SID per the VT-605 dedup
             convention) -> record the decision via tm_audit. ``skip_legacy_dispatch`` is ALWAYS
             False here — the caller's existing ``dispatch_brain`` call STILL runs, unconditionally,
             right after this; shadow NEVER answers/sends/effects anything of its own. A shadow plan
             persists status='shadow' (round-3 fix — task_store.TASK_ACTIVE excludes it), so it
             NEVER occupies the tenant's one-active-task admission slot and is NEVER claimed/driven
             — the plan CONTENT stays queryable for the divergence review without the row ever
             competing with a real turn.
  enforce -> triage OWNS routing for new_task (creates the plan; STARTS the durable workflow ONLY
             when create_plan actually admitted it as 'planned' — round-3 fix: a 'queued' result
             behind an already-active task has nothing to start yet, so it falls through to the
             legacy path rather than force-starting a workflow with no work to claim) and
             answer_pending (correlates the reply to the SPECIFIC open question found — round-3
             fix: bound to that question's own (task_id, question_id), never
             correlate_reply's implicit tenant-latest fallback). ``cancel_task``/``direct_reply``/
             ``task_status`` fall through to the legacy path (this row wires the ROUTING decision
             only for those three — a real cancellation/status/direct-reply pipeline is a documented
             gap, a later row's job). UNTESTED-LIVE until VT-611 gates enforce on anywhere; this
             module only proves the routing DECISION in isolation.

Triage's own fail-soft contract (triage.py) already covers "classifier errored/returned garbage" ->
None -> the caller falls back to the legacy path exactly as if mode were legacy for this turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from orchestrator.manager.loop_mode import LoopMode, get_loop_mode
from orchestrator.observability.tm_audit import emit_tm_audit

if TYPE_CHECKING:
    from orchestrator.manager.plan_models import ManagerPlan

logger = logging.getLogger("orchestrator.manager.triage_seam")


@dataclass(frozen=True, slots=True)
class TriageSeamResult:
    """What the seam decided. ``skip_legacy_dispatch`` tells the CALLER (runner.py) whether to
    still invoke ``dispatch_brain`` — only ever True in enforce mode for a new_task that was
    actually admitted 'planned' and started, or an answer_pending correlated against a real open
    question; legacy is ALWAYS False (untouched); shadow is ALWAYS False (never owns an effect)."""

    outcome: str | None
    task_id: UUID | None
    skip_legacy_dispatch: bool
    # Shared infra (D2/D3) — a deterministic in-turn reply the seam wants the runner to SEND this
    # turn (the seam is a plain fn inside a @DBOS.workflow, so it must NOT send itself — a naked send
    # would double-fire on replay). None (default) keeps every existing construction byte-compatible.
    direct_reply_text: str | None = None


_NO_OP = TriageSeamResult(outcome=None, task_id=None, skip_legacy_dispatch=False)


def _build_draft_plan(message_text: str) -> ManagerPlan:
    """The MINIMAL viable draft for a new_task classification: this row does not build a
    natural-language "plan drafting" capability (a separate, larger piece of work — see the VT-606
    completion report) — a single non-specialist step frames the owner's own ask for the durable
    loop to work from. Opus's plan-validation checkpoint judges THIS draft, not a sophisticated
    multi-step plan; a later row that builds real plan drafting slots in ahead of validation
    without changing the checkpoint itself."""
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from orchestrator.privacy.pii_redactor import redact

    safe_text = redact(message_text) or message_text
    return ManagerPlan(
        objective=str(safe_text)[:500],
        # VT-633 — structurally FALSIFIABLE constants (a log row / a DB record — checkable by the
        # verification cycle), replacing "the owner confirms the ask was addressed", which the
        # per-turn opus validator intermittently rejected as unfalsifiable (see the checkpoint
        # note in _create_plan_for_new_task).
        acceptance_criteria=[
            "an owner-visible reply addressing this ask is recorded in the conversation log "
            "for this task",
            "any business effect claimed to the owner (campaign, send, approval) has a "
            "matching DB record",
        ],
        steps=[
            PlanStep(
                step_seq=1,
                kind="clarification",
                situation=str(safe_text)[:500],
                desired_outcome="Understand and act on the owner's request.",
            )
        ],
    )


def _build_campaign_recovery_plan(message_text: str) -> ManagerPlan:
    """D3 — a sales_recovery specialist_dispatch plan for a clear win-back imperative against a
    REAL cohort. ONE dispatch step to ``sales_recovery_agent`` so the loop RUNS the win-back
    (a clear imperative was already detected — no clarification round-trip). NO
    ``allowed_effect_classes``: every existing SR plan omits it, and the actual send still routes
    through the proposal-time approval / consent / opt-out rails exactly as any SR send does — this
    plan grants NO effect bypass, it only makes the ROUTING deterministic (VT-633 variance root)."""
    from orchestrator.manager.plan_models import ManagerPlan, PlanStep
    from orchestrator.privacy.pii_redactor import redact

    safe_text = redact(message_text) or message_text
    return ManagerPlan(
        objective=str(safe_text)[:500],
        acceptance_criteria=[
            "an owner-visible reply addressing this win-back ask is recorded in the conversation "
            "log for this task",
            "any campaign/send claimed to the owner has a matching DB record",
        ],
        steps=[
            PlanStep(
                step_seq=1,
                kind="specialist_dispatch",
                specialist="sales_recovery_agent",
                situation=str(safe_text)[:500],
                desired_outcome="Run a win-back campaign to the tenant's lapsed customers.",
            )
        ],
    )


def _create_plan_for_new_task(
    tenant_id: UUID, message_text: str, message_sid: str, *, shadow: bool,
) -> UUID | None:
    """opus plan-validation checkpoint (A5) BEFORE create_plan. A validation failure fails SOFT —
    returns None, no plan created, no exception — the caller treats a None task_id exactly like
    "triage didn't classify this as new_task", falling back to the legacy dispatch.

    ``shadow`` passes straight through to ``plan_store.create_plan`` (round-3 fix: a shadow-mode
    plan must persist status='shadow', never 'planned'/'queued', so it can never occupy the
    tenant's one-active-task admission slot — see the module docstring).

    VT-633 — the MINIMAL TEMPLATE draft is validated BY CONSTRUCTION, not per turn. This function
    only ever builds the one fixed template (_build_draft_plan: constant falsifiable criteria +
    a single clarification step; the only variance is the redacted owner text in objective/
    situation, which the checkpoint was never judging for safety — consent/approval gates live
    elsewhere). Per-turn opus judgment of that CONSTANT was a coin flip in the hot path: the same
    template drew valid=true on one run and "unfalsifiable" on the next (tm_audit 2026-07-10
    02:23:06, against the validator prompt's own "an owner confirmation" example), silently
    routing the SAME owner ask to enforce on one run and legacy dispatch on the next — the
    variance at the root of the delegation-lane failures. The opus checkpoint (A5) REMAINS the
    gate for any future non-template drafted plan; it is simply not re-asked about a constant."""
    from orchestrator.manager import plan_store

    draft = _build_draft_plan(message_text)
    emit_tm_audit(
        event_layer="decides", event_kind="triage_plan_template_prevalidated", actor="team_manager",
        tenant_id=tenant_id,
        summary="minimal template draft — validated by construction (VT-633), no per-turn LLM call",
        decision={"message_sid": message_sid},
    )
    return plan_store.create_plan(
        tenant_id, draft, source_message_sid=message_sid, shadow=shadow
    )


def triage_seam(
    tenant_id: UUID, message_text: str, message_sid: str, *, mode: LoopMode | None = None,
) -> TriageSeamResult:
    """The dispatch-seam entry point. Called from runner.webhook_pipeline_run immediately before
    its own dispatch_brain call; the RETURN's ``skip_legacy_dispatch`` tells that caller whether to
    still invoke dispatch_brain this turn."""
    resolved_mode = mode if mode is not None else get_loop_mode()
    if resolved_mode == "legacy":
        return _NO_OP  # zero new calls — not even triage.triage_turn is imported below this line

    from orchestrator.manager import pending_questions, task_store
    from orchestrator.manager.triage import triage_turn

    open_questions = pending_questions.get_open(tenant_id)
    has_open_question = bool(open_questions)
    has_active_task = task_store.has_active_task(tenant_id)

    # DF5 — the pre-brain deterministic COUNT/STATUS answer net. A status/count QUESTION ("how many
    # lapsed customers?", "what's the status?") is ANSWERED in-turn from DB truth, bypassing the
    # fragile Haiku classifier that in enforce routes it to an async specialist spawn
    # (loop_stall/ignored_speech_act). answer_status_query returns None for anything it doesn't own
    # (incl. field mutations "update my city" — guarded — and unknowns), so it falls through cleanly.
    # LIST/NAMES asks are excluded (a count is not a list — that's the CD2 attachment path). All reads
    # are RLS-scoped + read-only (money-safe); the reply sends via the runner's replay-safe step.
    # FAIL-OPEN: any error falls through to triage_turn.
    if resolved_mode == "enforce" and not has_active_task:
        try:
            from orchestrator.onboarding.campaign_first_contact import _INTERROGATIVE_LEAD_RE
            from orchestrator.owner_inputs.status_query import answer_status_query

            _low = (message_text or "").lower()
            _is_list_or_names_ask = any(k in _low for k in ("list", "names", "naam"))
            # QUESTION-SHAPE gate (hook-caught regression): classify_status_query is bag-of-words, so
            # an IMPERATIVE like "win back lapsed customers" hits the 'lapsed' token and would get a
            # COUNT answer instead of a task. The net answers QUESTIONS only: a '?', an interrogative
            # lead (how/what/kya/kitne/…), or a count-cue token. Imperatives fall through to D3/triage.
            _toks = set(_low.replace("?", " ").split())
            _is_question_shaped = (
                "?" in (message_text or "")
                or bool(_INTERROGATIVE_LEAD_RE.match(message_text or ""))
                or bool(_toks & {"kitne", "kitni", "kitna", "how"})
            )
            if _is_question_shaped and not _is_list_or_names_ask:
                _status_ans = answer_status_query(tenant_id, message_text)
                if _status_ans is not None:
                    emit_tm_audit(
                        event_layer="decides", event_kind="status_answer_in_turn",
                        actor="team_manager", tenant_id=tenant_id,
                        summary="deterministic count/status answer in-turn (pre-brain, no async spawn)",
                        decision={"message_sid": message_sid},
                    )
                    return TriageSeamResult(
                        outcome="direct_reply", task_id=None, skip_legacy_dispatch=True,
                        direct_reply_text=_status_ans,
                    )
        except Exception:  # noqa: BLE001 — the DF5 net must never block the turn (fail-open)
            logger.warning(
                "DF5 status net failed tenant=%s (fail-open -> triage_turn)", tenant_id, exc_info=True
            )

    # D3 (subsumes cluster-5b) — the deterministic CAMPAIGN first-contact net. A clear "run a
    # win-back campaign" imperative (enforce mode, no active task already owning the tenant) is
    # routed HERE rather than left to the intermittent classifier below. Two honest, deterministic
    # outcomes: an EMPTY customer ledger -> a grounded "no one to reach out to" reply + NO dispatch
    # (kills the "I've started a win-back" fabrication against a no-data tenant); a real cohort ->
    # mint a sales_recovery dispatch + start the durable workflow (the loop's approval/consent/
    # opt-out rails still gate every send — this changes ROUTING, never the money gates). FAIL-OPEN:
    # any error falls through to triage_turn below, exactly as if the net weren't here.
    if resolved_mode == "enforce" and not has_active_task:
        try:
            from orchestrator.onboarding.campaign_first_contact import (
                EMPTY_COHORT_REPLY,
                campaign_cohort_is_empty,
                is_campaign_plan_imperative,
            )

            if is_campaign_plan_imperative(message_text):
                if campaign_cohort_is_empty(tenant_id):
                    emit_tm_audit(
                        event_layer="decides", event_kind="campaign_first_contact_empty_cohort",
                        actor="team_manager", tenant_id=tenant_id,
                        summary="win-back imperative but customer ledger is empty — honest no-data "
                        "reply, no dispatch",
                        decision={"message_sid": message_sid},
                    )
                    return TriageSeamResult(
                        outcome="new_task", task_id=None, skip_legacy_dispatch=True,
                        direct_reply_text=EMPTY_COHORT_REPLY,
                    )
                # Real cohort — deterministically mint a sales_recovery dispatch + start the loop.
                from orchestrator.manager import plan_store
                from orchestrator.manager.workflow import start_manager_task_workflow

                camp_task_id = plan_store.create_plan(
                    tenant_id, _build_campaign_recovery_plan(message_text),
                    source_message_sid=message_sid, shadow=False,
                )
                camp_row = (
                    task_store.get_task(tenant_id, camp_task_id)
                    if camp_task_id is not None
                    else None
                )
                if camp_row is not None and str(camp_row["status"]) == "planned":
                    emit_tm_audit(
                        event_layer="decides", event_kind="campaign_first_contact_dispatched",
                        actor="team_manager", tenant_id=tenant_id,
                        summary="win-back imperative + real cohort — deterministic sales_recovery "
                        "dispatch",
                        decision={"message_sid": message_sid, "task_id": str(camp_task_id)},
                    )
                    start_manager_task_workflow(tenant_id, camp_task_id)
                    return TriageSeamResult(
                        outcome="new_task", task_id=camp_task_id, skip_legacy_dispatch=True,
                    )
                # create_plan didn't admit 'planned' — fall through to triage_turn (never silent).
        except Exception:  # noqa: BLE001 — the D3 net must never block the turn (fail-open)
            logger.warning(
                "D3 campaign first-contact net failed tenant=%s (fail-open -> triage_turn)",
                tenant_id, exc_info=True,
            )

    result = triage_turn(
        message_text=message_text,
        has_open_question=has_open_question,
        has_active_task=has_active_task,
    )
    if result is None:
        # triage.py's own fail-soft contract — classify failure falls back to the legacy dispatch
        # behavior for THIS turn, exactly as if the mode were legacy. VT-633: emit the audit row —
        # this fallback was INVISIBLE (the 02:24:39 approval turn rode legacy with no trace),
        # and an unaudited routing flip is exactly what made the delegation lane un-debuggable.
        emit_tm_audit(
            event_layer="decides", event_kind="triage_classify_failed", actor="team_manager",
            tenant_id=tenant_id,
            summary="triage classify failed (fail-soft) — this turn falls back to legacy dispatch",
            decision={"message_sid": message_sid, "mode": resolved_mode},
        )
        return _NO_OP

    task_id: UUID | None = None
    task_status: str | None = None
    if result.outcome == "new_task":
        task_id = _create_plan_for_new_task(
            tenant_id, message_text, message_sid, shadow=(resolved_mode == "shadow")
        )
        if task_id is not None:
            task_row = task_store.get_task(tenant_id, task_id)
            task_status = str(task_row["status"]) if task_row is not None else None

    emit_tm_audit(
        event_layer="decides",
        event_kind="triage_decision",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"triage classified '{result.outcome}' (mode={resolved_mode})",
        decision={
            "outcome": result.outcome,
            "mode": resolved_mode,
            "task_id": str(task_id) if task_id is not None else None,
            "task_status": task_status,
            "message_sid": message_sid,
            # §7D — the classifier's own stated WHY, not just the WHAT.
            "reasoning": result.reasoning,
        },
    )

    if resolved_mode == "shadow":
        # Shadow NEVER owns an effect — the plan-creation write above is inert DB state (no reply,
        # no send); the caller's existing dispatch_brain call still runs, unconditionally.
        return TriageSeamResult(outcome=result.outcome, task_id=task_id, skip_legacy_dispatch=False)

    # enforce
    if result.outcome == "new_task":
        if task_id is not None and task_status == "planned":
            from orchestrator.manager.workflow import start_manager_task_workflow

            start_manager_task_workflow(tenant_id, task_id)
            return TriageSeamResult(outcome=result.outcome, task_id=task_id, skip_legacy_dispatch=True)
        # 'queued' (an already-active task holds the slot) or plan-validation failed (task_id is
        # None): nothing to start yet either way. Recorded above via tm_audit; fall through to the
        # legacy path so the owner isn't left silent this turn — a dedicated "you're queued" reply
        # is a documented gap (no reply-path built for that state in this row).
        return TriageSeamResult(outcome=result.outcome, task_id=task_id, skip_legacy_dispatch=False)
    if result.outcome == "answer_pending":
        if open_questions:
            # Bound to the SPECIFIC open question found (oldest first, asked_at ASC) — never
            # correlate_reply's own implicit "most-recent-for-tenant" fallback, which could
            # resolve the wrong task's question if more than one happened to be open at once.
            target: dict[str, Any] = open_questions[0]
            pending_questions.correlate_reply(
                tenant_id, message_text, message_sid,
                question_id=target["id"], task_id=target["task_id"],
            )
            return TriageSeamResult(
                outcome=result.outcome, task_id=target["task_id"], skip_legacy_dispatch=True
            )
        # No open question found (a race/staleness between the has_open_question read above and
        # here) — nothing to correlate against; fall through rather than guessing.
        return TriageSeamResult(outcome=result.outcome, task_id=None, skip_legacy_dispatch=False)
    # cancel_task / direct_reply / task_status: this row wires the ROUTING decision only — a real
    # cancellation/status/direct-reply pipeline for enforce mode is a documented gap (a later row's
    # job, per the VT-606 completion report). Fall through to the legacy path.
    return TriageSeamResult(outcome=result.outcome, task_id=None, skip_legacy_dispatch=False)


__all__ = ["TriageSeamResult", "triage_seam"]
