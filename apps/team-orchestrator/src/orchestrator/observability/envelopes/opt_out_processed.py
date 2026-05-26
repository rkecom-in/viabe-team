"""VT-179 envelope: ``opt_out_processed`` (customer STOP / opt-out keyword)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class OptOutProcessedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sender_phone_token: str
    detected_keyword: str


class OptOutProcessedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suppression_recorded: bool
    suppression_id: str | None = None


class OptOutProcessedEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "opt_out_processed"

    input_envelope: OptOutProcessedInput
    output_envelope: OptOutProcessedOutput


__all__ = [
    "OptOutProcessedInput",
    "OptOutProcessedOutput",
    "OptOutProcessedEnvelope",
]
