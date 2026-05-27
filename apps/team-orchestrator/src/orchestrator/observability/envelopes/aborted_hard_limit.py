"""VT-179 envelope: ``aborted_hard_limit`` (VT-193 brain-wired termination).

Emitted when ``OrchestratorAgentDriver`` / ``OrchestratorReasoningCallback``
raises ``HardLimitExceeded`` mid-invocation (token / tool-call / depth /
wall-clock / cost limit hit). Pillar 8 error-taxonomy: this is a clean
terminal state, NOT an exception that DBOS retries.

Per VT-193 brief: ``status='aborted_hard_limit'`` (NOT `'escalated'`)
on the pipeline_runs row + this envelope row in pipeline_steps so the
Ops Console replay can render the offending limit + observed value
without scraping error envelopes.

Per VT-125: the structured ``HardLimitExceeded`` exception carries
``axis``, ``observed``, ``limit``, ``run_id``, ``tenant_id`` — this
envelope projects them as per-field columns (CL-417).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


HardLimitAxis = Literal[
    "tool_calls", "tokens", "wall_clock_s", "cost_paise", "depth"
]


class AbortedHardLimitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str  # human-readable summary
    inbound_body_len: int = 0


class AbortedHardLimitOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    axis: HardLimitAxis
    observed: float
    limit: float


class AbortedHardLimitEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "aborted_hard_limit"

    input_envelope: AbortedHardLimitInput
    output_envelope: AbortedHardLimitOutput


__all__ = [
    "HardLimitAxis",
    "AbortedHardLimitInput",
    "AbortedHardLimitOutput",
    "AbortedHardLimitEnvelope",
]
