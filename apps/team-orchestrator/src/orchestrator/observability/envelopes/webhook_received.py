"""VT-179 envelope: ``webhook_received`` (inbound Twilio webhook landed).

Step 0 of every pipeline_run. ``input_envelope`` carries the redacted
WebhookEvent fields (sender_phone is a phone_token, body PII-redacted via
VT-104). ``output_envelope`` is None — the step records arrival, not
processing.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class WebhookReceivedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body_token: str
    sender_phone_token: str
    message_type: Literal["inbound_message", "status_callback"]
    twilio_message_sid: str | None = None
    status_callback_state: str | None = None
    dupe_status: bool = False
    num_media: int = 0


class WebhookReceivedEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "webhook_received"

    input_envelope: WebhookReceivedInput
    output_envelope: None = None


__all__ = ["WebhookReceivedInput", "WebhookReceivedEnvelope"]
