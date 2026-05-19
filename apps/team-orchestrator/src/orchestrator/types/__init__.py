"""Shared orchestrator I/O types.

WebhookEvent is a MINIMAL STUB — VT-3.3 expands/replaces it when the Twilio
adapter ships. (The VT-3.8 ``Tenant`` stub was removed in VT-3.2 — subscriber
context is now ``orchestrator.state.SubscriberState``.)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WebhookEvent(BaseModel):
    """Inbound webhook event. Expanded in VT-3.3a with Twilio ingress fields."""

    body: str = ""
    sender_phone: str = ""
    message_type: Literal["inbound_message", "status_callback", "unknown"] = (
        "inbound_message"
    )
    twilio_message_sid: str | None = None
    status_callback_state: (
        Literal["delivered", "read", "failed", "undelivered"] | None
    ) = None
    # VT-3.3a Twilio ingress fields.
    dupe_status: bool = False  # True when the ingress layer saw this MessageSid before
    num_media: int = 0  # Twilio NumMedia — image/media attachment count
    media_url_0: str | None = None  # Twilio MediaUrl0, when num_media > 0


class RouteToDirectHandler(BaseModel):
    """Routine event — handle deterministically via a direct handler, no brain."""

    kind: Literal["direct_handler"] = "direct_handler"
    handler_name: str
    payload: dict = Field(default_factory=dict)


class RouteToBrain(BaseModel):
    """Event needs orchestrator-agent (Opus 4.7) reasoning — Stage 2, VT-3.9."""

    kind: Literal["brain"] = "brain"
    reason: str


class Reject(BaseModel):
    """Event is not the gate's concern — a duplicate, a signature failure, or an
    observability-only status callback."""

    kind: Literal["reject"] = "reject"
    reason: str


# Stage 1 routing outcome.
PreFilterResult = RouteToDirectHandler | RouteToBrain | Reject
