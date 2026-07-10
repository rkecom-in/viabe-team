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
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict

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
    "TerminalOutcome",
    "VerificationVerdict",
    "deterministic_floor_ok",
    "resolve_terminal_outcome",
    "verify_completion",
]
