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
    """Notify the owner of a failed template send and record retry-eligibility.

    VT-564: a 'failed' status callback for a CUSTOMER send is ALSO reconciled against the
    customer-send ledger (delivery_status + reviewer alert) as a fail-soft FIRST step that never
    regresses the owner error-notification below. A no-op when the sid is not a customer send
    (an owner-notification failure, an unknown sid)."""
    # VT-564 — reconcile the customer-send delivery ledger for this 'failed' callback. Fully
    # fail-soft inside reconcile_customer_send_delivery; the owner notification below is unaffected.
    from orchestrator.agents.customer_send import reconcile_customer_send_delivery

    reconciled = reconcile_customer_send_delivery(
        state["tenant_id"], event.twilio_message_sid, event.status_callback_state
    )

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
        "reconciled": reconciled.matched,
        "send_result": send_result.model_dump(),
    }
