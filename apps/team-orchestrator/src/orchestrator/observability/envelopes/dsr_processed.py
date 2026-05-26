"""VT-179 envelope: ``dsr_processed`` (DPDP Data Subject Request purge)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class DsrProcessedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_phone_token: str
    request_type: Literal["delete", "export"]


class DsrProcessedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_affected: int
    tables_touched: list[str]
    completed_at_utc: str


class DsrProcessedEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "dsr_processed"

    input_envelope: DsrProcessedInput
    output_envelope: DsrProcessedOutput


__all__ = [
    "DsrProcessedInput",
    "DsrProcessedOutput",
    "DsrProcessedEnvelope",
]
