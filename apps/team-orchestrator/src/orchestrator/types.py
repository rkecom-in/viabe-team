"""Shared orchestrator types for the Pre-Filter Gate (VT-3.8).

WebhookEvent and Tenant are MINIMAL STUBS:
- VT-3.8 minimal stub — VT-3.3 will expand/replace WebhookEvent when the Twilio
  adapter ships.
- VT-3.8 minimal stub — VT-3.2 will expand/replace Tenant as part of
  SubscriberState.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class WebhookEvent(BaseModel):
    """Inbound webhook event. VT-3.8 minimal stub — VT-3.3 expands/replaces it."""

    body: str = ""
    sender_phone: str = ""
    message_type: Literal["inbound_message", "status_callback", "unknown"] = (
        "inbound_message"
    )
    twilio_message_sid: str | None = None
    status_callback_state: (
        Literal["delivered", "read", "failed", "undelivered"] | None
    ) = None


class Tenant(BaseModel):
    """Tenant context. VT-3.8 minimal stub — VT-3.2 expands/replaces it."""

    tenant_id: UUID
    opt_out_status: bool = False
    preferred_language: Literal["en", "hi"] = "en"


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
