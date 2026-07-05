"""VT-606 (team-lead ruling round 2) — wires ``manager.triage`` into the live dispatch seam
(``runner.webhook_pipeline_run``, immediately before its ``dispatch_brain`` call), mode-gated at
the read site so each mode's behavior is exactly what the team-lead ruling specifies:

  legacy  -> ZERO new calls. The hot path is BYTE-IDENTICAL to pre-VT-606 — ``triage_seam`` returns
             immediately without even IMPORTING ``triage.triage_turn``, let alone calling it. Pinned
             by ``test_triage_seam.py::test_legacy_mode_never_calls_triage``.
  shadow  -> triage runs OBSERVATIONALLY: classify -> (new_task: opus-validate a minimal draft plan
             -> create_plan, keyed on the inbound message SID per the VT-605 dedup convention:
             source_message_sid == the same pointer other seams call source_message_ref) -> record
             the decision via tm_audit. ``skip_legacy_dispatch`` is ALWAYS False here — the caller's
             existing ``dispatch_brain`` call STILL runs, unconditionally, right after this; shadow
             NEVER answers/sends/effects anything of its own (the plan-creation write is inert DB
             state, not a reply). Tested: no send originates from the shadow path.
  enforce -> triage OWNS routing for new_task (creates + STARTS the durable workflow) and
             answer_pending (correlates the reply) — ``skip_legacy_dispatch=True`` for those, so the
             caller skips its own ``dispatch_brain`` call. ``cancel_task``/``direct_reply``/
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
from typing import TYPE_CHECKING
from uuid import UUID

from orchestrator.manager.loop_mode import LoopMode, get_loop_mode
from orchestrator.observability.tm_audit import emit_tm_audit

if TYPE_CHECKING:
    from orchestrator.manager.plan_models import ManagerPlan

logger = logging.getLogger("orchestrator.manager.triage_seam")


@dataclass(frozen=True, slots=True)
class TriageSeamResult:
    """What the seam decided. ``skip_legacy_dispatch`` tells the CALLER (runner.py) whether to
    still invoke ``dispatch_brain`` — only ever True in enforce mode for new_task/answer_pending;
    legacy is ALWAYS False (untouched); shadow is ALWAYS False (never owns an effect)."""

    outcome: str | None
    task_id: UUID | None
    skip_legacy_dispatch: bool


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
        acceptance_criteria=["the owner confirms the ask was addressed"],
        steps=[
            PlanStep(
                step_seq=1,
                kind="clarification",
                situation=str(safe_text)[:500],
                desired_outcome="Understand and act on the owner's request.",
            )
        ],
    )


def _create_plan_for_new_task(
    tenant_id: UUID, message_text: str, message_sid: str
) -> UUID | None:
    """opus plan-validation checkpoint (A5) BEFORE create_plan. A validation failure fails SOFT —
    returns None, no plan created, no exception — the caller treats a None task_id exactly like
    "triage didn't classify this as new_task", falling back to the legacy dispatch."""
    from orchestrator.manager import plan_store
    from orchestrator.manager.plan_validation import validate_plan_draft

    draft = _build_draft_plan(message_text)
    try:
        validation = validate_plan_draft(draft)
    except Exception:  # noqa: BLE001 — fail-soft, never drop the turn over a validation crash
        logger.warning("triage_seam: plan-validation call raised (fail-soft, no plan created)")
        emit_tm_audit(
            event_layer="decides", event_kind="triage_plan_validation_error", actor="team_manager",
            tenant_id=tenant_id, summary="plan-validation call raised; falling back to legacy dispatch",
            decision={"message_sid": message_sid},
        )
        return None

    if not validation.valid:
        emit_tm_audit(
            event_layer="decides", event_kind="triage_plan_validation_failed", actor="team_manager",
            tenant_id=tenant_id,
            summary=f"draft plan rejected: {validation.reason}",
            decision={"message_sid": message_sid, "reason": validation.reason},
        )
        return None

    return plan_store.create_plan(tenant_id, draft, source_message_sid=message_sid)


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

    has_open_question = bool(pending_questions.get_open(tenant_id))
    has_active_task = task_store.has_active_task(tenant_id)
    result = triage_turn(
        message_text=message_text,
        has_open_question=has_open_question,
        has_active_task=has_active_task,
    )
    if result is None:
        # triage.py's own fail-soft contract — classify failure falls back to the legacy dispatch
        # behavior for THIS turn, exactly as if the mode were legacy.
        return _NO_OP

    task_id: UUID | None = None
    if result.outcome == "new_task":
        task_id = _create_plan_for_new_task(tenant_id, message_text, message_sid)

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
            "message_sid": message_sid,
        },
    )

    if resolved_mode == "shadow":
        # Shadow NEVER owns an effect — the plan-creation write above is inert DB state (no reply,
        # no send); the caller's existing dispatch_brain call still runs, unconditionally.
        return TriageSeamResult(outcome=result.outcome, task_id=task_id, skip_legacy_dispatch=False)

    # enforce
    if result.outcome == "new_task":
        if task_id is not None:
            from orchestrator.manager.workflow import start_manager_task_workflow

            start_manager_task_workflow(tenant_id, task_id)
            return TriageSeamResult(outcome=result.outcome, task_id=task_id, skip_legacy_dispatch=True)
        return TriageSeamResult(outcome=result.outcome, task_id=None, skip_legacy_dispatch=False)
    if result.outcome == "answer_pending":
        pending_questions.correlate_reply(tenant_id, message_text, message_sid)
        return TriageSeamResult(outcome=result.outcome, task_id=None, skip_legacy_dispatch=True)
    # cancel_task / direct_reply / task_status: this row wires the ROUTING decision only — a real
    # cancellation/status/direct-reply pipeline for enforce mode is a documented gap (a later row's
    # job, per the VT-606 completion report). Fall through to the legacy path.
    return TriageSeamResult(outcome=result.outcome, task_id=None, skip_legacy_dispatch=False)


__all__ = ["TriageSeamResult", "triage_seam"]
