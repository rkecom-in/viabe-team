"""VT-606 (Loop Package 3) — the manager_review node.

Two phases per Package 3's diagram ("consume structured result -> Manager review decision"):

  1. STRUCTURED EXTRACTION — ONE sonnet-5 LLM call (amendment A5: "manager triage + review nodes
     default to the Sonnet-5 tier"). A specialist's raw graph output (messages / tool calls /
     campaign_plan) is not itself a validated return — real specialists emit whatever their own
     sub-graph produces. ``extract_specialist_return`` reads that raw output + the step's
     acceptance_criteria and produces a ``PlanSpecialistReturn`` (manager/plan_models.py) — a
     grounded, "trust but verify" read of what actually happened. Mirrors
     ``agent.tools.classify_owner_message``'s house pattern EXACTLY: a raw ``Anthropic().messages.
     create`` call + JSON parse + pydantic validation (NOT ``with_structured_output`` — unused
     anywhere in this codebase; this keeps one convention, testable with a mock client).

  2. DETERMINISTIC DECISION SEAM — no LLM. The extracted ``PlanSpecialistReturn`` bridges (amendment
     A1's adapter, ``to_legacy_specialist_return``) into ``roster.SpecialistReturn`` (the LIVE,
     dormant VT-526 dataclass — untouched, never replaced, per A1) and feeds
     ``manager.decision.decide_next_action`` (existing, pure, tested — REUSED, not reinvented).
     ``ManagerReviewOutcome``'s docstring below documents the exact mapping from Package 3's SIX
     named outcomes onto ``decide_next_action``'s five.

Consent note: this call transmits the SPECIALIST's already-produced output (not fresh owner text).
By the time a specialist has been dispatched at all, the SAME turn already passed
``runner._brain_owner_inputs_ok`` (VT-303/CL-425) upstream of ``dispatch_brain`` — this module does
NOT re-check consent; re-implementing it here would duplicate the gate without adding safety (the
turn could not have reached a specialist dispatch without it already having passed).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from anthropic import Anthropic

from orchestrator.manager import plan_store, task_store
from orchestrator.manager.decision import ManagerDecision, ManagerDecisionKind, decide_next_action
from orchestrator.manager.plan_models import EvidenceRef, PlanSpecialistReturn
from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit

if TYPE_CHECKING:
    from orchestrator.agent.roster import SpecialistReturn

logger = logging.getLogger("orchestrator.manager.review")

# A5: the manager's triage + review nodes default to the Sonnet-5 tier. SAME model id as
# agent.dispatch._BRAIN_MODEL_SONNET (single source of truth would import it, but dispatch.py has
# heavy langgraph/langchain deps this module must stay free of for the dep-less smoke suite — the
# id string is the actual contract, pinned here + asserted equal to dispatch's constant by a test).
_REVIEW_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 600

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_review_extraction.md"
_EXTRACTION_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n?(?P<body>.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE
)


def _strip_code_fence(raw: str) -> str:
    match = _CODE_FENCE_RE.match(raw)
    return match.group("body").strip() if match is not None else raw


def extract_specialist_return(
    *,
    situation: str,
    desired_outcome: str,
    acceptance_criteria: list[str],
    raw_output: str,
    client: Anthropic | None = None,
) -> PlanSpecialistReturn:
    """The ONE sonnet-5 LLM call: turn a specialist's raw output into a grounded, validated
    ``PlanSpecialistReturn``. Raises ``ValueError`` on non-JSON / schema-invalid output — the
    caller (``manager_review``) treats an extraction failure as fail-closed ``blocked`` (never a
    silent guess at what happened)."""
    if client is None:
        client = Anthropic()

    user_content = (
        f"## Situation\n{situation}\n\n"
        f"## Desired outcome\n{desired_outcome}\n\n"
        f"## Acceptance criteria\n"
        + "\n".join(f"- {c}" for c in acceptance_criteria)
        + f"\n\n## Specialist raw output\n{raw_output}"
    )
    resp = client.messages.create(
        model=_REVIEW_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "".join(text_blocks).strip()
    if not raw:
        raise ValueError("extract_specialist_return: model returned empty content")
    raw = _strip_code_fence(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"extract_specialist_return: model returned non-JSON: {raw[:200]!r}"
        ) from exc
    try:
        return PlanSpecialistReturn(**parsed)
    except Exception as exc:  # noqa: BLE001 — surfaced as a typed ValueError for the caller
        raise ValueError(
            f"extract_specialist_return: envelope validation failed: {parsed}"
        ) from exc


def to_legacy_specialist_return(ret: PlanSpecialistReturn) -> "SpecialistReturn":
    """Amendment A1 adapter — bridge the NEW ``PlanSpecialistReturn`` into the LIVE, dormant
    ``roster.SpecialistReturn`` dataclass so ``decide_next_action`` (VT-526, untouched) can decide.

    Mapping (documented, not guessed):
      - ``status in {'blocked', 'failed'}`` with a ``proposed_outcome`` -> pushback=True (REVISE
        path — the specialist proposes a better outcome instead of the infeasible one).
      - ``status in {'blocked', 'failed'}`` with NO ``proposed_outcome`` -> pushback=True, empty
        proposed_outcome (ESCALATE path — decide_next_action escalates when there's no path).
      - ``status == 'needs_owner_input'`` -> pushback=False, action_taken="" (decide_next_action's
        "nothing actionable" branch -> CLARIFY/ask_owner).
      - ``status == 'completed'`` -> pushback=False, action_taken=ret.action_summary,
        outcome=ret.outcome_summary (decide_next_action's ACCEPT/NEXT_SPECIALIST branch).
    """
    from orchestrator.agent.roster import SpecialistReturn

    if ret.status in ("blocked", "failed"):
        return SpecialistReturn(
            pushback=True,
            proposed_outcome=ret.proposed_outcome or "",
            reason=ret.reason_code or ret.outcome_summary or "",
        )
    if ret.status == "needs_owner_input":
        return SpecialistReturn(pushback=False, action_taken="", outcome=ret.outcome_summary)
    return SpecialistReturn(
        pushback=False, action_taken=ret.action_summary, outcome=ret.outcome_summary
    )


# ManagerReviewOutcome — Package 3's SIX named branches, verbatim. This implementation only ever
# PRODUCES {continue, complete, revise_step, ask_owner, escalate} — never a bare "accept_step" —
# because decide_next_action already folds "persist this step's evidence" INTO whichever of
# {continue, complete} applies (its NEXT_SPECIALIST / ACCEPT outcomes respectively); Package 3's
# diagram lists "accept_step -> persist evidence" as the EFFECT of a successful step, which happens
# as part of both continue and complete here, not as an independently reachable third branch. The
# type still declares all six literals (a truthful public contract, and future-proof if a caller
# ever needs the bare accept-without-progression case).
ManagerReviewOutcome = Literal[
    "accept_step", "revise_step", "ask_owner", "continue", "complete", "escalate"
]

_DECISION_TO_OUTCOME: dict[ManagerDecisionKind, ManagerReviewOutcome] = {
    ManagerDecisionKind.ACCEPT: "complete",
    ManagerDecisionKind.NEXT_SPECIALIST: "continue",
    ManagerDecisionKind.REVISE: "revise_step",
    ManagerDecisionKind.CLARIFY: "ask_owner",
    ManagerDecisionKind.ESCALATE: "escalate",
}


class ManagerReviewResult:
    """The full record of one manager_review pass — everything a caller/test needs to introspect
    without re-deriving it."""

    __slots__ = ("outcome", "specialist_return", "decision", "incident_id")

    def __init__(
        self,
        *,
        outcome: ManagerReviewOutcome,
        specialist_return: PlanSpecialistReturn,
        decision: ManagerDecision,
        incident_id: UUID | None = None,
    ) -> None:
        self.outcome = outcome
        self.specialist_return = specialist_return
        self.decision = decision
        self.incident_id = incident_id

    def __repr__(self) -> str:  # pragma: no cover — debug convenience only
        return (
            f"ManagerReviewResult(outcome={self.outcome!r}, "
            f"decision={self.decision.kind.value!r}, incident_id={self.incident_id})"
        )


def manager_review(
    tenant_id: UUID | str,
    task_id: UUID | str,
    step_id: UUID | str,
    *,
    situation: str,
    desired_outcome: str,
    acceptance_criteria: list[str],
    raw_output: str,
    has_next_step: bool,
    client: Anthropic | None = None,
) -> ManagerReviewResult:
    """The manager_review node (Package 3): extract -> decide -> persist the plan_store effect +
    tm_audit + (escalate only) a VTR incident. Never silent: an extraction failure itself is
    treated as a ``blocked``/escalate outcome (fail-closed), never swallowed.
    """
    try:
        ret = extract_specialist_return(
            situation=situation,
            desired_outcome=desired_outcome,
            acceptance_criteria=acceptance_criteria,
            raw_output=raw_output,
            client=client,
        )
    except ValueError as exc:
        logger.warning(
            "manager_review: structured extraction failed for step=%s (fail-closed -> escalate): %s",
            step_id, exc,
        )
        ret = PlanSpecialistReturn(
            status="failed",
            action_summary="",
            outcome_summary="manager_review could not extract a structured result",
            reason_code="extraction_failed",
        )

    legacy_ret = to_legacy_specialist_return(ret)
    decision = decide_next_action(legacy_ret, has_next_step=has_next_step)

    # VT-606 round-3 MINOR fix (adversarial review): a CLARIFY decision with NO owner_question
    # text must NEVER park the step 'waiting' — nothing would ever answer a question that was
    # never actually asked (pending_questions.ask below is skipped when owner_question is empty),
    # so the task would sit at 'waiting_owner' forever with no path to resume. Redirect to
    # revise_step instead — governed by the SAME per-step revision budget workflow.py's own limit
    # enforces (never a silent busy-spin/infinite loop; the budget check there naturally escalates
    # once exhausted). dataclasses.replace (ManagerDecision is frozen) so ManagerReviewResult.decision
    # accurately reflects the decision actually ACTED upon, not the pre-correction CLARIFY.
    if decision.kind is ManagerDecisionKind.CLARIFY and not ret.owner_question:
        decision = dataclasses.replace(
            decision,
            kind=ManagerDecisionKind.REVISE,
            reason="clarify_with_no_question_text",
            revised_outcome=(
                f"{desired_outcome} — if you still cannot proceed without asking the owner, "
                "state the EXACT question to ask this time."
            ),
        )

    outcome = _DECISION_TO_OUTCOME[decision.kind]

    incident_id: UUID | None = None
    evidence = ret.evidence_refs[0] if ret.evidence_refs else None
    plan_store_evidence = (
        EvidenceRef(kind=evidence.kind, ref=evidence.ref) if evidence is not None else None
    )

    if outcome in ("continue", "complete"):
        plan_store.complete_step(
            tenant_id, step_id, "done",
            evidence=plan_store_evidence, expected_from=("running",),
        )
        if outcome == "complete":
            task_store.set_task_status(tenant_id, task_id, "verifying", expected_from=("running",))
    elif outcome == "revise_step":
        # The step returns to pending to re-run with the revised outcome (mirrors
        # decide_next_action/record_decision's REVISE handling) — the CALLER (manager_task_workflow)
        # owns actually building + persisting the revised ManagerPlan via plan_store.revise_plan;
        # this seam only marks the current step re-runnable and records the decision.
        task_store.set_step_status(tenant_id, step_id, "pending", expected_from=("running",))
    elif outcome == "ask_owner":
        task_store.set_step_status(tenant_id, step_id, "waiting", expected_from=("running",))
        task_store.set_task_status(tenant_id, task_id, "waiting_owner", expected_from=("running",))
        if ret.owner_question:
            from orchestrator.manager import pending_questions

            pending_questions.ask(
                tenant_id, ret.owner_question, task_id=task_id, question_kind="clarification",
            )
    elif outcome == "escalate":
        task_store.set_step_status(tenant_id, step_id, "failed", expected_from=("running",))
        task_store.set_task_status(
            tenant_id, task_id, "blocked", expected_from=tuple(task_store.TASK_NON_TERMINAL)
        )
        # incident_store.create_incident is idempotent per (run_id, incident_kind); task_id is a
        # soft (no-FK) correlation key — reused here as the "run_id" slot so a repeat escalate for
        # the SAME task never double-creates (mirrors specialist_return._enforce_escalate's use of
        # the SAME incident_store seam, one tier further: to_tier=2 goes straight to VTR).
        iid = create_incident(
            tenant_id,
            incident_kind="other",
            run_id=task_id,
            severity="warning",
            detail={
                "source": "manager_review",
                "task_id": str(task_id),
                "step_id": str(step_id),
                "reason": ret.reason_code or decision.reason,
            },
        )
        if iid is not None:
            escalate_incident(tenant_id, iid, to_tier=2)
            incident_id = iid

    emit_tm_audit(
        event_layer="decides",
        event_kind="manager_review_decision",
        actor="team_manager",
        tenant_id=tenant_id,
        summary=f"manager_review: step={step_id} status={ret.status!r} -> outcome={outcome!r}",
        decision={
            "task_id": str(task_id),
            "step_id": str(step_id),
            "specialist_status": ret.status,
            "outcome": outcome,
            "decision_kind": decision.kind.value,
            "reason": decision.reason,
        },
    )

    return ManagerReviewResult(
        outcome=outcome, specialist_return=ret, decision=decision, incident_id=incident_id
    )


__all__ = [
    "ManagerReviewOutcome",
    "ManagerReviewResult",
    "extract_specialist_return",
    "manager_review",
    "to_legacy_specialist_return",
]
