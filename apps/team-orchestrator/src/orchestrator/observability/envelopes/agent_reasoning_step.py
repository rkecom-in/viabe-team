"""VT-179 envelope: ``agent_reasoning_step`` (one Anthropic Messages SDK turn).

Per CL-249 — mirrors what the Messages SDK returns: think_text, action,
action_args, tokens_input/output, model_used. Consumed by VT-182's SDK
callback for OTel emission.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class AgentReasoningStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_token_count: int


class AgentReasoningStepOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    think_text: str | None = None
    action: str | None = None
    action_args: dict[str, Any] | None = None
    # VT-182 — logfire span trace_id at the time of this Messages.create
    # round-trip; None when Logfire is disabled or no active span.
    # Surfaced via opentelemetry.trace.get_current_span() in agent_callback.
    logfire_trace_id: str | None = None


class AgentReasoningStepEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "agent_reasoning_step"

    input_envelope: AgentReasoningStepInput
    output_envelope: AgentReasoningStepOutput


__all__ = [
    "AgentReasoningStepInput",
    "AgentReasoningStepOutput",
    "AgentReasoningStepEnvelope",
]
