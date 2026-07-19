"""VT-606 (team-lead ruling round 2) — the opus plan-validation checkpoint.

Amendment A5's FIRST named opus call ("plan validation on objective creation"; the other is
completion-verification, manager/verification.py). Runs on a DRAFT ``ManagerPlan`` BEFORE
``plan_store.create_plan`` persists it — a validation failure fails SOFT to the current dispatch
behavior (the caller falls back to the legacy path; never a dropped turn), recorded via tm_audit.

Two of the three things the team-lead ruling named ("schema-valid" and "steps within roster") are
ALREADY enforced STRUCTURALLY — a malformed dict never constructs a ``ManagerPlan`` at all
(pydantic's own validators: sequential step_seq, specialist-iff-specialist_dispatch, and
``PlanStep.specialist``'s ``SpecialistName`` Literal type already rejects anything outside the
3-specialist roster). This checkpoint's real job is the ONE thing structure cannot check: are the
acceptance criteria genuinely MEASURABLE (not vague/unfalsifiable) and do the steps plausibly serve
the objective.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from orchestrator.llm.structured import structured_text_call
from orchestrator.manager.plan_models import ManagerPlan

logger = logging.getLogger("orchestrator.manager.plan_validation")

# A5: plan-validation-at-objective-creation is one of the loop's ONLY two opus calls (the other is
# completion-verification). It runs on the "review" tier (TEAM_MODEL_REVIEW; default
# claude-opus-4-8). VT-619b pinned this to the Anthropic SDK; it is now routed through the
# multi-provider seam (structured_text_call) so the tier can be pointed at any provider and the
# call is cost-metered.
_VALIDATION_TIER = "review"
_MAX_TOKENS = 400

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_plan_validation.md"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class PlanValidationResult(BaseModel):
    """The opus checkpoint's structured verdict. ``extra='forbid'`` — an unrecognized field is a
    schema drift, never silently accepted."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    reason: str = ""


def validate_plan_draft(
    plan: ManagerPlan, *, text_call: Callable[..., str] | None = None,
) -> PlanValidationResult:
    """NEVER raises. A client/parse/schema failure fails SOFT to ``valid=False`` with a reason
    describing what went wrong — the caller's own contract ("validation failure -> fail-soft to
    current dispatch behavior, never a dropped turn") means an extraction failure here must look
    exactly like a genuine 'not valid' to the caller, never a crash that would drop the turn."""
    user_content = json.dumps(
        {
            "objective": plan.objective,
            "acceptance_criteria": plan.acceptance_criteria,
            "steps": [
                {
                    "step_seq": s.step_seq,
                    "kind": s.kind,
                    "specialist": s.specialist,
                    "situation": s.situation,
                    "desired_outcome": s.desired_outcome,
                    "acceptance_criteria": s.acceptance_criteria,
                }
                for s in plan.steps
            ],
        },
        default=str,
    )

    _call = text_call or structured_text_call
    try:
        text = _call(
            _VALIDATION_TIER,
            system=_SYSTEM_PROMPT,
            user=user_content,
            max_tokens=_MAX_TOKENS,
            agent="plan_validation",
            call_site="plan_validation",
            tenant_id=None,
        )
        if not text.strip():
            raise ValueError("empty response from plan-validation call")
        cleaned = _FENCE_RE.sub("", text).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"non-JSON plan-validation response: {exc}") from exc
        return PlanValidationResult(**parsed)
    except Exception as exc:  # noqa: BLE001 — "never a dropped turn": ANY failure (a raised
        # network/API error included, not just a parse/schema mismatch) fails soft to invalid.
        logger.warning(
            "validate_plan_draft: extraction failed (fail-soft -> invalid): %s", exc,
        )
        return PlanValidationResult(valid=False, reason=f"plan_validation_extraction_failed:{type(exc).__name__}")


__all__ = ["PlanValidationResult", "validate_plan_draft"]
