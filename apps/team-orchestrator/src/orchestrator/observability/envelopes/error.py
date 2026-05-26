"""VT-179 envelope: ``error`` (failure-classifier router decision).

Replaces legacy step_kind ``error_router_decision`` (VT-179 Option A
canonical rename — error_router.py writes this kind). Wraps the canonical
``pipeline_steps.error`` JSONB column structure as the single source of
truth (CL-417 / VT-187).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


FailureType = Literal[
    "tool_call_timeout",
    "tool_call_validation_failure",
    "llm_api_error",
    "llm_invalid_response",
    "owner_clarification_required",
    "hard_limit_reached",
    "webhook_signature_failure",
    "tenant_isolation_breach",
    "context_truncation",
    "unknown",
]


Strategy = Literal[
    "retry_with_backoff",
    "retry_after_owner_clarification",
    "escalate_to_owner",
    "escalate_to_fazal",
    "accept_and_log",
]


class ErrorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_type: FailureType
    message: str
    vendor: str | None = None
    retry_count: int = 0


class ErrorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Strategy


class ErrorEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "error"

    input_envelope: ErrorInput
    output_envelope: ErrorOutput


__all__ = [
    "FailureType",
    "Strategy",
    "ErrorInput",
    "ErrorOutput",
    "ErrorEnvelope",
]
