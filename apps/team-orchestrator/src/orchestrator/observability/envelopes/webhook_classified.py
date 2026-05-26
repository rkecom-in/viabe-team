"""VT-179 envelope: ``webhook_classified`` (intent + tenant resolution decision)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class WebhookClassifiedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sender_phone_token: str
    body_token: str


class WebhookClassifiedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Literal[
        "owner_command",
        "customer_reply",
        "status_callback",
        "unknown",
        "rejected_unknown_sender",
    ]
    tenant_resolved: bool
    classifier_used: Literal["deterministic", "llm"]


class WebhookClassifiedEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "webhook_classified"

    input_envelope: WebhookClassifiedInput
    output_envelope: WebhookClassifiedOutput


__all__ = [
    "WebhookClassifiedInput",
    "WebhookClassifiedOutput",
    "WebhookClassifiedEnvelope",
]
