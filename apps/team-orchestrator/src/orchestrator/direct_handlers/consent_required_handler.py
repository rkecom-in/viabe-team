"""consent_required_handler — VT-303 Option-B graceful degrade.

Fires when an inbound owner message would route to the brain but the tenant has
NOT enabled ``owner_inputs`` (the lawful basis for transmitting the owner's
message — which may carry customer PII — to Anthropic, CL-425). Instead of
transmitting, we send a conservative NON-LLM reply pointing the owner to the
enable path. No Anthropic call happens.

Pillar 1: fully deterministic, zero LLM.
Pillar 7 (owner-truth): the prompt send is the honest outcome (send_result),
never a hardcoded claim.
CL-390: the send util hashes the recipient phone in logs; we never log it raw.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_freeform_message

# The enable phrase the owner must send back. Kept in sync with the FIRST entry
# of config/data_inputs_enable_keywords.yaml (the canonical, human-facing grant
# phrase). Surfaced here so the prompt tells the owner exactly what to send.
_ENABLE_PHRASE = "ACTIVATE TEAM"

_CONSENT_PROMPT = (
    "Your AI team is ready, but it needs your go-ahead before it can read your "
    "business data and start working.\n\n"
    f"Reply *{_ENABLE_PHRASE}* to enable data inputs and activate it.\n\n"
    "Enabling lets your AI team process your messages and customer data to "
    "recover sales for you. You can pause anytime by replying STOP."
)


@DBOS.step()
def consent_required_handler(
    event: WebhookEvent, state: SubscriberState
) -> dict[str, Any]:
    """Send the conservative enable-prompt; never transmit to the brain."""
    sid: str | None = None
    error: str | None = None
    recipient = event.sender_phone or None
    if recipient is not None:
        try:
            sid = send_freeform_message(_CONSENT_PROMPT, recipient)
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            error = repr(exc)
    else:
        error = "no recipient phone on event"

    return {
        "handler": "consent_required_handler",
        "consent_prompt_sent": sid is not None,
        "send_result": {"sid": sid, "error": error},
    }
