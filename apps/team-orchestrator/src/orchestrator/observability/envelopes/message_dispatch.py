"""VT-179 envelope: ``message_dispatch`` (Twilio Content API send)."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class MessageDispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipient_phone_token: str
    template_name: str
    content_sid: str
    variables: dict[str, Any]


class MessageDispatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    twilio_message_sid: str
    delivery_state: Literal["queued", "sent", "delivered", "failed"]


class MessageDispatchEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "message_dispatch"

    input_envelope: MessageDispatchInput
    output_envelope: MessageDispatchOutput


__all__ = [
    "MessageDispatchInput",
    "MessageDispatchOutput",
    "MessageDispatchEnvelope",
]
