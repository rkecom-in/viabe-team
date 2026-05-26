"""VT-179 envelope: ``day39_evaluator`` (day-39 cumulative-fees check)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class Day39EvaluatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paid_conversion_at_utc: str
    cumulative_fees_paise: int
    arrr_paise: int


class Day39EvaluatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["refund_triggered", "no_refund"]
    multiplier_threshold: int
    decided_at_utc: str


class Day39EvaluatorEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "day39_evaluator"

    input_envelope: Day39EvaluatorInput
    output_envelope: Day39EvaluatorOutput


__all__ = [
    "Day39EvaluatorInput",
    "Day39EvaluatorOutput",
    "Day39EvaluatorEnvelope",
]
