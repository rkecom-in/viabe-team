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

    # Anthropic input (prompt) token count for this Messages.create turn —
    # the canonical reasoning-step input measure (CL-249). Populated from the
    # response usage.input_tokens by the agent_callback writer.
    prompt_token_count: int
    # VT-464 D4: the Context Composer bundle provenance the agent_callback
    # writer actually emits. These were previously absent from the schema,
    # so with extra="forbid" every brain reasoning-step envelope soft-failed
    # validation (payload_validation_failed=True) and Ops replay was degraded.
    # Declaring them here makes the brain envelope validate without weakening
    # the strict forbid posture (still no UNDECLARED extras).
    context_bundle_hash: str
    context_bundle_components: list[str]
    context_bundle_token_count: int
    prior_tool_calls_count: int
    prior_tool_calls_summary: list[dict[str, Any]]


class AgentReasoningStepOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    think_text: str | None = None
    action: str | None = None
    action_args: dict[str, Any] | None = None
    # VT-182 — logfire span trace_id at the time of this Messages.create
    # round-trip; None when Logfire is disabled or no active span.
    # Surfaced via opentelemetry.trace.get_current_span() in agent_callback.
    logfire_trace_id: str | None = None
    # VT-194 — Anthropic prompt-caching observability. Default 0 keeps
    # backward compatibility with pre-VT-194 rows (CL-417 per-field
    # column shape). First dispatch within TTL: cache_creation_input_tokens > 0.
    # Subsequent dispatches within TTL: cache_read_input_tokens > 0.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class AgentReasoningStepEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "agent_reasoning_step"

    input_envelope: AgentReasoningStepInput
    output_envelope: AgentReasoningStepOutput


__all__ = [
    "AgentReasoningStepInput",
    "AgentReasoningStepOutput",
    "AgentReasoningStepEnvelope",
]
