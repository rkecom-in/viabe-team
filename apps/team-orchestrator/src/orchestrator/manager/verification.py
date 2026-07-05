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

logger = logging.getLogger("orchestrator.manager.verification")

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


def resolve_terminal_outcome(steps: list[dict[str, Any]]) -> TerminalOutcome:
    """The evidence-presence proxy for "did this task produce a real effect" — see the module
    docstring for why this is a proxy, not a true executed-effect signal."""
    return "completed_with_effect" if any(s.get("evidence_kind") for s in steps) else "completed_no_action"


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
