"""VT-179 envelope: ``self_evaluate_gate`` (agent self-evaluation verdict).

Per CL-281 verdict-model widening (all-reasons model) + CL-278 REVISE
contract. Replaces legacy step_kind ``self_evaluate_attempt``
(VT-179 Option A canonical rename — sales_recovery writes this kind).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


SelfEvaluateVerdict = Literal[
    "pass",
    "fail",
    "revise",
    "needs_owner_clarification",
    "escalate_fazal",
]


class SelfEvaluateGateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_output: dict[str, Any]
    evaluator_prompt_version: str


class SelfEvaluateGateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: SelfEvaluateVerdict
    reasons: list[str]
    retry_carry: dict[str, Any] | None = None


class SelfEvaluateGateEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "self_evaluate_gate"

    input_envelope: SelfEvaluateGateInput
    output_envelope: SelfEvaluateGateOutput


__all__ = [
    "SelfEvaluateVerdict",
    "SelfEvaluateGateInput",
    "SelfEvaluateGateOutput",
    "SelfEvaluateGateEnvelope",
]
