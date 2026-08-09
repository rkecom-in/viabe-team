"""VT-606 (Loop Package 3) — the manager_review node.

Two phases per Package 3's diagram ("consume structured result -> Manager review decision"):

  1. STRUCTURED EXTRACTION — ONE LLM call on the manager's review tier (amendment A5: "manager triage
     + review nodes default to the Sonnet-5 tier" — VT-732 makes that a TIER, so which model the tier
     means is the env's call, per environment). A specialist's raw graph output (messages / tool calls
     / campaign_plan) is not itself a validated return — real specialists emit whatever their own
     sub-graph produces. ``extract_specialist_return`` reads that raw output + the step's
     acceptance_criteria and produces a ``PlanSpecialistReturn`` (manager/plan_models.py) — a
     grounded, "trust but verify" read of what actually happened. Mirrors ``manager.triage``'s house
     pattern EXACTLY: ``structured_text_call`` + JSON parse + pydantic validation (NOT
     ``with_structured_output`` — unused anywhere in this codebase; this keeps one convention,
     testable by injecting ``text_call``).

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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from orchestrator.agent.schemas.campaign_plan import CampaignStatus
from orchestrator.llm.structured import structured_text_call
from orchestrator.manager import plan_store, task_store
from orchestrator.manager.decision import ManagerDecision, ManagerDecisionKind, decide_next_action
from orchestrator.manager.plan_models import EffectIntent, EvidenceRef, PlanSpecialistReturn
from orchestrator.observability.incident_store import create_incident, escalate_incident
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.knowledge_contracts import grounding_audit_payload

if TYPE_CHECKING:
    from orchestrator.agent.roster import SpecialistReturn
    from orchestrator.agent.schemas.campaign_plan import CampaignPlan
    from orchestrator.agent_framework.context import ModuleResult

logger = logging.getLogger("orchestrator.manager.review")

# A5: the manager's triage + review nodes run on the SAME tier (parity with manager.triage's
# _TRIAGE_TIER, which is what the "same model as the brain" assertion was really pinning). VT-732 —
# the tier NAME is the contract now; the concrete model comes from TEAM_MODEL_COMPLEX at call time,
# so dev and prod can differ without a code change and neither can drift from the brain by accident.
_REVIEW_TIER = "complex"
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
    text_call: Callable[..., str] | None = None,
) -> PlanSpecialistReturn:
    """The ONE review-tier LLM call: turn a specialist's raw output into a grounded, validated
    ``PlanSpecialistReturn``. Raises ``ValueError`` on non-JSON / schema-invalid output — the
    caller (``manager_review``) treats an extraction failure as fail-closed ``blocked`` (never a
    silent guess at what happened).

    VT-732 — ``text_call`` replaces the old injectable ``client``: the transport is now the
    multi-provider seam (which also owns sampling: temperature is pinned only where the resolved
    model accepts it), so a test injects a callable, not an SDK stub."""
    _call = text_call or structured_text_call

    user_content = (
        f"## Situation\n{situation}\n\n"
        f"## Desired outcome\n{desired_outcome}\n\n"
        f"## Acceptance criteria\n"
        + "\n".join(f"- {c}" for c in acceptance_criteria)
        + f"\n\n## Specialist raw output\n{raw_output}"
    )
    raw = _call(
        _REVIEW_TIER,
        system=_EXTRACTION_SYSTEM_PROMPT,
        user=user_content,
        max_tokens=_MAX_TOKENS,
        agent="manager_review",
        call_site="specialist_return_extraction",
    ).strip()
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
            evidence_manifest=tuple(ret.evidence_manifest),
            conflict_manifest=tuple(ret.conflict_manifest),
            knowledge_version=ret.knowledge_version,
            grounding_status=ret.grounding_status,
        )
    if ret.status == "needs_owner_input":
        return SpecialistReturn(
            pushback=False,
            action_taken="",
            outcome=ret.outcome_summary,
            evidence_manifest=tuple(ret.evidence_manifest),
            conflict_manifest=tuple(ret.conflict_manifest),
            knowledge_version=ret.knowledge_version,
            grounding_status=ret.grounding_status,
        )
    return SpecialistReturn(
        pushback=False,
        action_taken=ret.action_summary,
        outcome=ret.outcome_summary,
        evidence_manifest=tuple(ret.evidence_manifest),
        conflict_manifest=tuple(ret.conflict_manifest),
        knowledge_version=ret.knowledge_version,
        grounding_status=ret.grounding_status,
    )


def preserve_module_grounding(
    ret: PlanSpecialistReturn, module_result: "ModuleResult"
) -> PlanSpecialistReturn:
    """Copy O8 provenance from the framework envelope into Manager synthesis input.

    The business status/outcome remains the already-validated specialist return.  This adapter only
    carries content-free provenance fields and reconstructs the Pydantic model so its grounding
    invariants are re-run (``model_copy(update=...)`` would skip validation).
    """

    payload = ret.model_dump()
    payload.update(
        evidence_manifest=list(module_result.evidence_manifest),
        conflict_manifest=list(module_result.conflict_manifest),
        knowledge_version=module_result.knowledge_version,
        grounding_status=module_result.grounding_status,
    )
    return PlanSpecialistReturn.model_validate(payload)


def _cohort_ids_are_grounded(tenant_id: UUID | str, customer_ids: list[UUID]) -> bool:
    """VT-607 (Loop Package 6) grounding check: do EVERY one of the plan's cohort customer_ids
    resolve to a REAL, tenant-scoped ``customers`` row? Schema validation (``CampaignPlanProposed``)
    only guarantees the list is well-SHAPED (non-empty UUIDs, cohort_size matches len) — it cannot
    catch a HALLUCINATED cohort (ids the model invented that don't exist for this tenant, or belong
    to a different one). ``collapse_node``'s own ``resolve_cohort_recipients`` would catch this too,
    but only AFTER manager_review has already decided to accept the step — this check runs the SAME
    existence test, read-only (no campaign_recipients write — collapse hasn't run yet), BEFORE that
    decision, so an ungrounded cohort never gets marked 'done' on a plan that was never going to
    survive collapse."""
    if not customer_ids:
        return True
    from orchestrator.db.wrappers import CustomersWrapper

    unique_ids = {str(c) for c in customer_ids}
    # Wrapper-layer read (VT-72/306 no-direct-tenant-db-access gate) — the count of
    # REAL tenant-scoped rows must equal the number of distinct proposed ids.
    n = CustomersWrapper().count_existing(tenant_id, list(unique_ids))
    return n == len(unique_ids)


def adapt_campaign_plan_to_specialist_return(
    tenant_id: UUID | str, plan: "CampaignPlan"
) -> PlanSpecialistReturn:
    """VT-607 (Loop Package 6) — the typed CampaignPlan-variant -> PlanSpecialistReturn adapter,
    feeding the SAME ``to_legacy_specialist_return`` bridge + ``decide_next_action`` decision seam
    every OTHER specialist's sonnet-5-extracted return does. Sales Recovery already produces a
    STRUCTURED, schema-validated artifact — asking an LLM to re-interpret it into ANOTHER structure
    (``extract_specialist_return``'s job for free-text specialists) would be unnecessary latency,
    cost and (mis-transcription) risk for zero benefit; this adapter is deterministic, no LLM call.

    Grounding (fail-closed, deterministic, evaluated here — never silently skipped):
      - PROPOSED: the cohort must be GROUNDED (``_cohort_ids_are_grounded`` — see its own
        docstring); ungrounded -> ``blocked``, no ``proposed_outcome`` (escalate — an operator-
        visible incident, never a silent drop or a step marked done on a plan collapse would have
        rejected anyway). A grounded plan's OWN schema already guarantees a non-empty cohort has
        non-empty evidence_refs + a populated expected_arrr (``CampaignPlanProposed``'s own
        ``Field(..., min_length=1)`` / ``ge=1`` constraints) — asserted again here, explicitly and
        defensively, rather than silently trusted, in case a future schema change ever loosens
        them. The plan's own selection_reason/basis (also non-empty by schema) is what "the
        requested outcome addressed" grounds on — the Manager's desired_outcome was threaded into
        the SR context bundle (handoffs._build_sales_recovery_update) specifically so the
        specialist has it to address in those prose fields.
      - OUT_OF_SCOPE: no ``proposed_outcome`` -> ``blocked``/escalate (SR genuinely cannot act
        in-lane on this ask; the Manager decides what happens next).
      - INSUFFICIENT_DATA: ``proposed_outcome`` derived from the plan's OWN ``missing_data`` items
        -> ``blocked``/revise (the Manager can reframe the ask or wait, governed by the EXISTING
        per-step revision budget — workflow.py's LIMIT_MAX_REVISIONS_PER_STEP, unchanged).
    """
    if plan.status == CampaignStatus.PROPOSED:
        cohort = plan.target_cohort
        if not cohort.customer_ids or not _cohort_ids_are_grounded(tenant_id, cohort.customer_ids):
            return PlanSpecialistReturn(
                status="blocked",
                outcome_summary=(
                    f"proposed cohort of {cohort.cohort_size} customer(s) does not resolve to "
                    "real, tenant-scoped customers — ungrounded"
                ),
                reason_code="ungrounded_cohort",
            )
        if not plan.evidence_refs or plan.expected_arrr is None:
            # Structurally unreachable for a valid CampaignPlanProposed (schema-enforced) —
            # defensive, explicit per the grounding spec, never silently trusted.
            return PlanSpecialistReturn(
                status="blocked",
                outcome_summary="proposed plan missing evidence_refs or expected_arrr",
                reason_code="ungrounded_plan",
            )
        evidence_refs = [
            EvidenceRef(kind="campaign_plan", ref=ref.source_id) for ref in plan.evidence_refs
        ]
        effect_intents = [
            EffectIntent(
                effect_class="customer_send",
                summary=(
                    f"Propose a recovery campaign to {cohort.cohort_size} customer(s) "
                    f"({cohort.cohort_label})"
                ),
                magnitude_minor=plan.expected_arrr.low_paise,
            )
        ]
        return PlanSpecialistReturn(
            status="completed",
            action_summary=(
                f"Proposed a recovery campaign for {cohort.cohort_size} customer(s): "
                f"{cohort.selection_reason}"
            ),
            outcome_summary=plan.expected_arrr.basis,
            evidence_refs=evidence_refs,
            effect_intents=effect_intents,
        )
    if plan.status == CampaignStatus.OUT_OF_SCOPE:
        return PlanSpecialistReturn(
            status="blocked",
            outcome_summary=plan.out_of_scope_reason,
            reason_code="out_of_scope",
        )
    # INSUFFICIENT_DATA — VT-738 RV-2.
    #
    # This used to return status='blocked' with proposed_outcome="address the following before
    # retrying: <gaps>". That string is not an OUTCOME the manager can re-frame toward, it is a
    # list of things that are missing — but the adapter above maps any blocked-with-a-proposed-
    # outcome onto pushback=True, and decide_next_action maps pushback-with-a-proposal onto REVISE.
    # So the manager reframed the step and re-dispatched it, the specialist found the same data
    # still missing, and it looped until `max_revisions_per_step_seq` blocked the task and triage
    # cancelled the plan. Observed on deployed dev 2026-08-10: three identical revisions, three
    # identical `insufficient_data` returns, task blocked, plan cancelled, owner told nothing
    # useful. **Re-framing a step cannot create data that does not exist, so that loop could never
    # converge** — it was not a tuning problem, it was the wrong branch.
    #
    # Missing data is the owner's to supply, so this is exactly what `needs_owner_input` exists
    # for: it routes through decide_next_action's CLARIFY branch, parks the task durably, and asks.
    # A spurious ask (the cohort read is separately flaky — RV-1) costs the owner one question; the
    # old path cost them the whole plan, silently.
    gaps = "; ".join(
        f"{item.category}: {item.description} (suggest: {item.suggested_remediation})"
        for item in plan.missing_data
    )
    remediations = [
        r for r in (str(item.suggested_remediation or "").strip() for item in plan.missing_data) if r
    ]
    if remediations:
        asks = "; ".join(remediations)
        return PlanSpecialistReturn(
            status="needs_owner_input",
            outcome_summary=f"insufficient data to propose a campaign: {gaps}",
            owner_question=(
                "I couldn't build the win-back campaign yet because some information is missing. "
                f"Could you help with this: {asks}?"
            ),
            reason_code="insufficient_data",
        )
    # No remediation the owner could act on → there is genuinely no path here. Return blocked with
    # NO proposed_outcome so decide_next_action ESCALATES (an honest closure) rather than looping.
    return PlanSpecialistReturn(
        status="blocked",
        outcome_summary=f"insufficient data to propose a campaign: {gaps}",
        reason_code="insufficient_data",
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
    text_call: Callable[..., str] | None = None,
    campaign_plan: "CampaignPlan | None" = None,
    run_id: UUID | str | None = None,
    module_result: "ModuleResult | None" = None,
) -> ManagerReviewResult:
    """The manager_review node (Package 3): extract -> decide -> persist the plan_store effect +
    tm_audit + (escalate only) a VTR incident. Never silent: an extraction failure itself is
    treated as a ``blocked``/escalate outcome (fail-closed), never swallowed.

    ``campaign_plan`` (VT-607, Loop Package 6): when the just-dispatched step is Sales Recovery and
    it produced a structured ``CampaignPlan``, the deterministic ``adapt_campaign_plan_to_
    specialist_return`` grounding + adapter REPLACES the review-tier ``extract_specialist_return``
    call entirely for THIS step (no LLM re-interpretation of already-structured, already-validated
    output — see that function's own docstring for the full grounding rationale). Every other
    specialist (``campaign_plan is None``) is completely unaffected — the review-tier extraction path
    below runs exactly as before.

    ``run_id`` (§7D): the caller's ACTIVE ObservabilityContext.run_id — NOT ``state['run_id']``.
    The two diverge for an enforce-loop dispatch (``manager.workflow._dispatch_specialist_step``
    enters ``observability_context(run_id=UUID(task_id), ...)`` while ``state['run_id']`` carries
    the per-attempt ``loop_run_id(task_id, step_id, attempt)``); the orchestrator_agent_turn
    reasoning_turn row this joins to is ALWAYS written under the former (``langchain_callback.py``
    reads ``ctx.run_id``, never ``state['run_id']``), so the caller must pass THAT value, not the
    loop's per-attempt one — see ``supervisor._manager_review_node``'s call site.
    """
    if campaign_plan is not None:
        ret = adapt_campaign_plan_to_specialist_return(tenant_id, campaign_plan)
    else:
        try:
            ret = extract_specialist_return(
                situation=situation,
                desired_outcome=desired_outcome,
                acceptance_criteria=acceptance_criteria,
                raw_output=raw_output,
                text_call=text_call,
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

    if module_result is not None:
        ret = preserve_module_grounding(ret, module_result)

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
        # owns actually building + persisting the replacement step via plan_store.replace_step
        # (round-3 MAJOR #4 fix — supersedes this step with one carrying decision.revised_outcome,
        # carrying every other non-superseded step forward; NOT plan_store.revise_plan, which would
        # re-insert the WHOLE plan as pending); this seam only marks the current step re-runnable
        # and records the decision.
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
            **grounding_audit_payload(
                evidence_manifest=tuple(ret.evidence_manifest),
                conflict_manifest=tuple(ret.conflict_manifest),
                knowledge_version=ret.knowledge_version,
                grounding_status=ret.grounding_status,
            ),
        },
        # §7D — joins back to the SAME turn's orchestrator_agent_turn reasoning_turn row (the
        # extraction call above reads that turn's specialist output; decision.reason is the
        # deterministic seam's OWN templated why, already carried in decision{} above).
        reasoning_ref=(
            {"run_id": str(run_id), "step_name": "orchestrator_agent_turn"}
            if run_id is not None
            else None
        ),
    )

    return ManagerReviewResult(
        outcome=outcome, specialist_return=ret, decision=decision, incident_id=incident_id
    )


__all__ = [
    "ManagerReviewOutcome",
    "ManagerReviewResult",
    "extract_specialist_return",
    "manager_review",
    "preserve_module_grounding",
    "to_legacy_specialist_return",
]
