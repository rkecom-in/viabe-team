"""VT-179 envelope: ``context_truncation`` (bundle section dropped under token cap).

Operational signal — context_builder truncates an optional bundle section
when the assembled bundle exceeds the effective token cap. Severity is
informational; the step still completes successfully.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class ContextTruncationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section: str


class ContextTruncationEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "context_truncation"

    input_envelope: ContextTruncationInput
    output_envelope: None = None


__all__ = ["ContextTruncationInput", "ContextTruncationEnvelope"]
