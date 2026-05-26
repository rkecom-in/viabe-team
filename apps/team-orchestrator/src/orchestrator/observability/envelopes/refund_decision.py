"""VT-179 envelope: ``refund_decision`` (refund issued post-day39 verdict)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class RefundDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    triggered_by_step_id: str
    arrr_paise: int


class RefundDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["queued", "issued", "failed"]
    refund_amount_paise: int
    issued_at_utc: str | None = None


class RefundDecisionEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "refund_decision"

    input_envelope: RefundDecisionInput
    output_envelope: RefundDecisionOutput


__all__ = [
    "RefundDecisionInput",
    "RefundDecisionOutput",
    "RefundDecisionEnvelope",
]
