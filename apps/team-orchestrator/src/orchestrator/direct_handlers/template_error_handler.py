"""template_error_handler — Pre-Filter direct handler for failed template
sends (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7: the return contract reports the real Twilio outcome of the
owner-notification send.

VT-3.6 (error-handling + retry framework) is not built yet. For now this
handler records retry-eligibility and sends the owner the error-notification
template; richer retry / escalation logic lands with VT-3.6.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_template_message


@DBOS.step()
def template_error_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Notify the owner of a failed template send and record retry-eligibility."""
    # Template send failures are transient and retry-eligible by default;
    # VT-3.6 will replace this flag with real retry / escalation logic.
    retry_eligible = True

    # Owner-audience template — no recipient override, so it goes to the
    # tenant's whatsapp_number (the owner) per VT-3.3c routing.
    send_result = send_template_message(
        state["tenant_id"],
        "team_error_handler",
        {},
    )

    return {
        "handler": "template_error_handler",
        "retry_eligible": retry_eligible,
        "send_result": send_result.model_dump(),
    }
