"""VT-606 (Loop Package 3, team-lead ruling round 2) — the completion-verification checkpoint.

Package 3's diagram: ``complete -> verify objective``. Without this, nothing the loop produces ever
reaches a TRUE ``task_store.TASK_TERMINAL`` status (queue promotion is dead code) and the loop
cannot honestly claim "the objective was achieved" — a specialist's own claim of "done" is not
independently checked. Amendment A5's OTHER named opus checkpoint (triage + manager_review stay
sonnet-5; plan-validation-at-objective-creation + THIS, completion-verification, are the ONLY two
opus calls in the whole loop).

DETERMINISTIC FLOOR — runs BEFORE any LLM call, never skipped: every 'done' step that declared its
own acceptance criteria (``PlanStep.acceptance_criteria``, persisted in
``manager_task_steps.detail``) must carry at least one recorded evidence
(``evidence_kind``/``evidence_ref``) — a step claiming to satisfy criteria with ZERO cited evidence
fails CLOSED without spending an opus call. Only once every declared-criteria step clears this does
the opus judgment call run at all.

TERMINAL_OUTCOME proxy (a documented judgment call, not a silent guess — flagged in the VT-606
completion report): ``PlanSpecialistReturn.effect_intents`` are PROPOSALS ONLY (plan_models.py's
own contract: "never execute directly") — nothing in this codebase executes one yet, so there is no
"was an effect intent executed" signal to read. The best available proxy today: ANY step in the
current plan_revision carrying a non-null ``evidence_kind`` (a real artifact/outcome was produced,
not pure analysis with nothing to show) -> ``completed_with_effect``; zero evidence across every
step -> ``completed_no_action``. A later row wiring real effect EXECUTION should replace this proxy
with the real "was an effect actually executed" signal.

VT-633 F-3 — the deterministic executed-effect FLOOR on top of that proxy: the proxy alone let a
``campaign_send`` approval settle ``completed_with_effect`` off the PROPOSAL step's own
``evidence_kind`` (written when the campaign PLAN was produced, before approval — never touched
again once the loop's F-2 execution step actually runs it), so an approved-but-never-executed
campaign (still 'proposed'/'approved', zero ``campaign_messages`` rows) reported the SAME false
success as one that really sent. ``resolve_terminal_outcome`` now downgrades that specific case to
``completed_no_action`` — a real "was it executed" check, not a replacement for the proxy (the
proxy still decides every non-campaign task; this floor only ever DOWNGRADES, never upgrades).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict

from orchestrator.llm.structured import structured_text_call
from orchestrator.manager import task_store
from orchestrator.manager.message_ids import loop_run_id

logger = logging.getLogger("orchestrator.manager.verification")

# VT-633 F-3 — the ceiling for the candidate run_id enumeration below: an upper bound on how many
# times the loop could ever redispatch the SAME step_id (an ask_owner resume reclaims the same
# step_id repeatedly; a revise_step/needs_changes supersession mints a BRAND NEW step_id instead,
# per message_ids.py's own note, so that path never grows this counter). workflow.py's own
# attempt_counts[step_id] increments at most once per outer-loop cycle, and cycles itself cannot
# exceed workflow.LIMIT_MAX_CYCLES (6) — so 1..6 covers every attempt value the loop could ever have
# minted for a step_id. A LOCAL constant, deliberately not imported from workflow.py: that module
# pulls in DBOS + the full graph stack, and this module stays import-light (test_verification.py's
# dep-less smoke coverage) — keep this in sync BY VALUE if workflow.LIMIT_MAX_CYCLES ever changes.
_MAX_ATTEMPT_CANDIDATES = 6

# A5: the completion-verification checkpoint is one of the loop's ONLY two opus calls (the other is
# plan-validation at objective creation, wired at create_plan's call site).
_VERIFICATION_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 500

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_completion_verification.md"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

VerificationVerdict = Literal["verified", "not_verified"]

TerminalOutcome = Literal["completed_with_effect", "completed_no_action"]


class CompletionVerification(BaseModel):
    """The opus checkpoint's structured verdict. ``extra='forbid'`` — an unrecognized field is a
    schema drift, never silently accepted."""

    model_config = ConfigDict(extra="forbid")

    verdict: VerificationVerdict
    reason: str = ""


# --- VT-680 (§7C) — the ONLINE impact judge --------------------------------------------------------
#
# ``verify_completion`` above checks that a task's work was DONE (evidence recorded, criteria
# structurally satisfied). It never checks whether the outcome was any GOOD against what the task
# actually set out to achieve — a completed-but-poor outcome passes silently today. This is that
# second, additive check: run ONLY after ``verify_completion`` has already returned "verified"
# (workflow.py's ``_run_verification_cycle`` is the call site), on the SAME persisted substrate
# (never a live specialist payload — replay/recovery semantics unchanged), advisory-only (v1: no
# auto-retry — see workflow.py's ``_judge_impact_step`` for why).

# opus-tier ("review" == TEAM_MODEL_REVIEW, default claude-opus-4-8) — the same tier class as this
# module's OWN completion-verification checkpoint above and plan_validation's sibling opus
# checkpoint. Routed through the multi-provider seam (``structured_text_call`` ->
# ``resolve_chat_model``), NOT a raw ``Anthropic()`` client like ``verify_completion`` above (a raw
# client bypasses the VT-619 cost-metering ledger — the audit finding this row's D4 responds to).
_IMPACT_JUDGE_TIER = "review"
_IMPACT_MAX_TOKENS = 400

ImpactJudgeVerdict = Literal["met", "partial", "unmet"]

# The online rubric is a STRICT SUBSET of tier_rescore's (the OFFLINE ×3 measurement judge) Tier-2
# quality dimensions: outcome-vs-desired_outcome + acceptance-criteria satisfaction ONLY — no tone/
# language/brevity (those are measurement-gate concerns, not per-task honesty). This comment BINDS
# the rubric to tier_rescore's Tier-2 categories (VT-680 D5) — a scope drift between the two becomes
# a diff away from visible instead of a silent divergence. The OFFLINE ×3 gate stays the promotion
# authority; this online judge is per-task honesty only, never a measurement metric itself.
_IMPACT_RUBRIC = """You are checking whether a completed business task actually achieved what it \
set out to do — NOT whether it merely finished (that was already verified separately; assume the \
task genuinely completed its steps).

You will be given the task's objective, its acceptance_criteria, and a summary of each step it \
executed — including each step's OWN desired_outcome and any evidence_kind it recorded.

Judge ONLY two things:
1. Did the real outcome match what each step's own desired_outcome asked for?
2. Were the objective's acceptance_criteria genuinely satisfied (not merely claimed)?

Do NOT judge tone, language, brevity, or anything else — those are a separate concern handled \
elsewhere.

Respond with ONLY a JSON object: {"verdict": "met" | "partial" | "unmet", "reason": "<one short \
sentence, plain language, no PII>"}
- "met": the outcome matches the desired outcome and the criteria are satisfied.
- "partial": the task technically completed but the outcome falls short of what was asked, or a
  criterion is only weakly evidenced.
- "unmet": the outcome does not match what was asked, or the criteria are not evidenced at all.
"""


class ImpactVerdict(BaseModel):
    """VT-680 (§7C) — the online impact judge's structured verdict: outcome/impact QUALITY against
    the task's own desired_outcome/acceptance_criteria, distinct from ``CompletionVerification``
    (which only checks the work was DONE). ``extra='forbid'`` mirrors ``CompletionVerification``.

    Deliberately has NO 'unjudged' member: a judge failure/timeout is a WORKFLOW-level fallback
    (``workflow._judge_impact_step`` catches and reports a plain ``"unjudged"`` string that never
    touches this pydantic type) — never a value this model itself represents."""

    model_config = ConfigDict(extra="forbid")

    verdict: ImpactJudgeVerdict
    reason: str = ""


def judge_impact(
    tenant_id: UUID | str, task_id: UUID | str, *, text_call: Callable[..., str] | None = None,
) -> ImpactVerdict:
    """The §7C online impact judge: scores the completed task's OUTCOME against its own
    desired_outcome/acceptance_criteria — quality/impact, never mere completion (``verify_completion``'s
    job, already passed by the time this is ever called). Reads the EXACT SAME persisted substrate
    ``verify_completion`` reads (task objective + acceptance_criteria + per-step summaries, including
    each step's OWN ``desired_outcome`` from its ``detail`` — never a live specialist payload, so
    replay/recovery semantics are unchanged).

    Routed through the multi-provider seam (``structured_text_call`` -> ``resolve_chat_model``), NOT
    a raw Anthropic client — VT-619 cost-metering free, luna-portable (the ``text_call=`` seam-port
    convention; mirrors ``plan_validation.validate_plan_draft``'s own port).

    Unlike its sibling checkpoints in this module (``verify_completion``, and
    ``plan_validation.validate_plan_draft``), this function does NOT fail-soft internally — it
    RAISES on any client/parse/schema failure. ``ImpactVerdict.verdict`` has no 'unjudged' member
    (this row's D1 Literal is exactly met/partial/unmet), so there is no value this function could
    honestly return for a failure; the caller (``workflow._judge_impact_step``) is the fail-soft
    boundary — it catches and reports a workflow-level 'unjudged' string that never touches this
    pydantic type, and the settle path proceeds exactly as if the judge had never run.
    """
    task = task_store.get_task(tenant_id, task_id)
    if task is None:
        raise ValueError(f"judge_impact: task not found tenant={tenant_id} task={task_id}")

    plan_revision = int(task.get("plan_revision") or 1)
    steps = _current_steps(tenant_id, task_id, plan_revision)

    objective_doc = task.get("objective") or {}
    objective = objective_doc.get("objective", "") if isinstance(objective_doc, dict) else ""
    criteria_doc = task.get("acceptance_criteria") or {}
    acceptance_criteria = (
        criteria_doc.get("acceptance_criteria", []) if isinstance(criteria_doc, dict) else []
    )
    step_summaries = [
        {
            "step_seq": s.get("step_seq"),
            "kind": s.get("kind"),
            "status": s.get("status"),
            "evidence_kind": s.get("evidence_kind"),
            "desired_outcome": (s.get("detail") or {}).get("desired_outcome") or "",
            "acceptance_criteria": (s.get("detail") or {}).get("acceptance_criteria") or [],
        }
        for s in steps
    ]
    user_content = json.dumps(
        {
            "objective": objective,
            "acceptance_criteria": acceptance_criteria,
            "steps": step_summaries,
        },
        default=str,
    )

    _call = text_call or structured_text_call
    text = _call(
        _IMPACT_JUDGE_TIER,
        system=_IMPACT_RUBRIC,
        user=user_content,
        max_tokens=_IMPACT_MAX_TOKENS,
        agent="team_manager",
        call_site="impact_judge",
        tenant_id=tenant_id,
    )
    if not text.strip():
        raise ValueError("empty response from impact-judge call")
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"non-JSON impact-judge response: {exc}") from exc
    return ImpactVerdict(**parsed)


def _current_steps(tenant_id: UUID | str, task_id: UUID | str, plan_revision: int) -> list[dict[str, Any]]:
    all_steps = task_store.get_steps(tenant_id, task_id)
    return [s for s in all_steps if s.get("plan_revision") == plan_revision]


def deterministic_floor_ok(steps: list[dict[str, Any]]) -> tuple[bool, str]:
    """Every 'done' step that declared its own acceptance criteria must carry evidence. Returns
    ``(ok, reason)`` — ``reason`` is empty when ``ok`` is True."""
    for step in steps:
        if step.get("status") != "done":
            continue
        detail = step.get("detail") or {}
        criteria = detail.get("acceptance_criteria") or []
        if criteria and not step.get("evidence_kind"):
            return False, f"step_seq={step.get('step_seq')} declared acceptance criteria with no recorded evidence"
    return True, ""


def resolve_terminal_outcome(
    tenant_id: UUID | str, task_id: UUID | str, steps: list[dict[str, Any]]
) -> TerminalOutcome:
    """The evidence-presence proxy for "did this task produce a real effect" (see the module
    docstring for why it's a proxy, not a true executed-effect signal) PLUS the VT-633 F-3
    deterministic downgrade: a proxy result of ``completed_with_effect`` is re-checked against
    whether an approved campaign this task's own dispatches minted actually got EXECUTED — if one
    is still stuck 'proposed'/'approved' with zero real sends, the proxy's "with effect" verdict is
    provably wrong and this downgrades to ``completed_no_action``. The downgrade check can only ever
    LOWER the proxy's verdict, never raise a ``completed_no_action`` to ``completed_with_effect``.

    Fail-soft (never crashes the loop): any read error during the downgrade check keeps the proxy
    result, logged as a warning — an unreadable DB is not grounds to change what the proxy already
    decided.
    """
    proxy = "completed_with_effect" if any(s.get("evidence_kind") for s in steps) else "completed_no_action"
    if proxy == "completed_no_action":
        return proxy  # nothing to downgrade — the floor only ever lowers this verdict

    try:
        from orchestrator.db.wrappers import CampaignsWrapper

        # Candidate run_ids: every loop_run_id this task's own steps could ever have dispatched
        # under (see _MAX_ATTEMPT_CANDIDATES above for why 1..6 is a safe, defensible ceiling — no
        # DB column records the real per-step attempt count, so this enumerates rather than reads).
        run_ids = [
            str(loop_run_id(task_id, step["id"], attempt))
            for step in steps
            if step.get("id") is not None
            for attempt in range(1, _MAX_ATTEMPT_CANDIDATES + 1)
        ]
        if run_ids and CampaignsWrapper().unexecuted_campaign_exists_for_runs(tenant_id, run_ids):
            return "completed_no_action"
    except Exception as exc:  # noqa: BLE001 — fail-soft: never crash the loop over this floor
        logger.warning(
            "resolve_terminal_outcome: executed-effect floor check failed for task=%s "
            "(keeping proxy result %r): %s", task_id, proxy, exc,
        )
    return proxy


def verify_completion(
    tenant_id: UUID | str, task_id: UUID | str, *, client: Anthropic | None = None,
) -> CompletionVerification:
    """The full checkpoint: deterministic floor, then (only if it passes) ONE opus judgment call.
    NEVER raises — a client/parse/schema failure fails CLOSED to ``not_verified`` with a reason
    describing what went wrong, mirroring ``manager_review``'s own fail-closed extraction discipline
    (an honest 'we could not verify this', never a crash and never a fabricated 'verified').
    """
    task = task_store.get_task(tenant_id, task_id)
    if task is None:
        return CompletionVerification(verdict="not_verified", reason="task_not_found")

    plan_revision = int(task.get("plan_revision") or 1)
    steps = _current_steps(tenant_id, task_id, plan_revision)

    floor_ok, floor_reason = deterministic_floor_ok(steps)
    if not floor_ok:
        return CompletionVerification(verdict="not_verified", reason=floor_reason)

    # VT-633 #51 — the NO-EFFECT fast path: a completed dispatch whose terminal outcome resolves
    # to 'completed_no_action' (no effect evidence, no campaign — e.g. an honest empty-cohort /
    # insufficient-data conclusion) has NOTHING effectful for the opus checkpoint to second-guess,
    # and the checkpoint could not pass anyway: the acceptance criterion "an owner-visible reply
    # is recorded" is satisfied BY the settle→notify that runs AFTER this verdict — a chicken-and-
    # egg the live pack surfaced as 2/3 empty-cohort runs leaving the owner in silence for minutes
    # (verify fails on the missing reply → retry churn → the honest "no action was needed" closure
    # only fires after the churn exhausts). Settle deterministically; the honest owner closure
    # then fires within seconds. Symmetric with the upward floor below: DB facts (here: the
    # ABSENCE of any effect) outrank LLM judgment.
    try:
        if resolve_terminal_outcome(tenant_id, task_id, steps) == "completed_no_action":
            return CompletionVerification(
                verdict="verified",
                reason="no-effect fast path: the dispatch concluded honestly with no business "
                       "effect (no campaign, no effect evidence) — nothing to verify beyond the "
                       "settle-notify this verdict releases",
            )
    except Exception as exc:  # noqa: BLE001 — fall through to the opus checkpoint, never crash
        logger.warning(
            "verify_completion: no-effect fast path failed for task=%s "
            "(falling through to the LLM checkpoint): %s", task_id, exc,
        )

    # VT-633 #52 — the deterministic UPWARD floor: a task whose own dispatch EXECUTED its
    # approved campaign (campaigns.status='sent' + real campaign_messages rows, DB-proven) is
    # VERIFIED — the opus judgment call is skipped entirely. Live defect this closes: the LLM
    # verifier (which cannot read the DB) second-guessed a successful sent:3 execution into a
    # retry → block → escalate, so the owner heard "Your campaign has gone out — I sent it to 3
    # customers" followed by "I couldn't finish it — I've flagged it for my team" — two
    # truthful-in-isolation lines that contradict each other. Symmetric with the F-3 DOWNWARD
    # floor in resolve_terminal_outcome: DB facts outrank LLM judgment in BOTH directions.
    # Fail-soft: a read error falls through to the normal opus checkpoint (never a fabricated
    # 'verified').
    try:
        from orchestrator.db.wrappers import CampaignsWrapper

        _run_ids = [
            str(loop_run_id(task_id, step["id"], attempt))
            for step in steps
            if step.get("id") is not None
            for attempt in range(1, _MAX_ATTEMPT_CANDIDATES + 1)
        ]
        if _run_ids and CampaignsWrapper().executed_campaign_exists_for_runs(tenant_id, _run_ids):
            return CompletionVerification(
                verdict="verified",
                reason="executed-effect floor: an approved campaign this task dispatched was "
                       "actually sent (campaigns 'sent' + recorded campaign_messages) — DB-proven",
            )
    except Exception as exc:  # noqa: BLE001 — fall through to the opus checkpoint, never crash
        logger.warning(
            "verify_completion: executed-effect upward floor check failed for task=%s "
            "(falling through to the LLM checkpoint): %s", task_id, exc,
        )

    objective_doc = task.get("objective") or {}
    objective = objective_doc.get("objective", "") if isinstance(objective_doc, dict) else ""
    criteria_doc = task.get("acceptance_criteria") or {}
    acceptance_criteria = (
        criteria_doc.get("acceptance_criteria", []) if isinstance(criteria_doc, dict) else []
    )
    step_summaries = [
        {
            "step_seq": s.get("step_seq"),
            "kind": s.get("kind"),
            "status": s.get("status"),
            "evidence_kind": s.get("evidence_kind"),
            "acceptance_criteria": (s.get("detail") or {}).get("acceptance_criteria") or [],
        }
        for s in steps
    ]
    user_content = json.dumps(
        {
            "objective": objective,
            "acceptance_criteria": acceptance_criteria,
            "steps": step_summaries,
        },
        default=str,
    )

    anthropic_client = client if client is not None else Anthropic()
    try:
        resp = anthropic_client.messages.create(
            model=_VERIFICATION_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise ValueError("empty response from completion-verification call")
        cleaned = _FENCE_RE.sub("", text).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"non-JSON completion-verification response: {exc}") from exc
        return CompletionVerification(**parsed)
    except Exception as exc:  # noqa: BLE001 — a raised network/API error fails closed exactly
        # like a parse/schema mismatch: never a crash, never a fabricated 'verified'.
        logger.warning(
            "verify_completion: extraction failed for task=%s (fail-closed -> not_verified): %s",
            task_id, exc,
        )
        return CompletionVerification(
            verdict="not_verified", reason=f"verification_extraction_failed:{type(exc).__name__}"
        )


__all__ = [
    "CompletionVerification",
    "ImpactJudgeVerdict",
    "ImpactVerdict",
    "TerminalOutcome",
    "VerificationVerdict",
    "deterministic_floor_ok",
    "judge_impact",
    "resolve_terminal_outcome",
    "verify_completion",
]
