"""customer_send_delivery_handler — Pre-Filter direct handler for customer-send
delivery status callbacks (VT-564).

Pillar 1: fully deterministic, zero LLM. Reconciles an async Twilio delivery callback
(delivered / read / undelivered) against the customer-send ledger (agent_customer_contacts):
'undelivered' is a delivery FAILURE — it stamps the ledger + fires the reviewer outbound_failure
alert; 'delivered' / 'read' record positive delivery evidence (no alert). The 'failed' state is
routed to ``template_error_handler`` instead (which ALSO reconciles), so the owner error-notification
that path already sends is preserved.

A callback whose sid is not a customer send (an owner notification, an unknown sid) is a silent
no-op — owner notifications reconcile in the runner (VT-524). All work is fail-soft inside
``reconcile_customer_send_delivery``; this handler never raises into the inbound webhook.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent


@DBOS.step()
def customer_send_delivery_handler(
    event: WebhookEvent, state: SubscriberState
) -> dict[str, Any]:
    """Reconcile a customer-send delivery callback against the ledger (fail-soft)."""
    from orchestrator.agents.customer_send import reconcile_customer_send_delivery

    result = reconcile_customer_send_delivery(
        state["tenant_id"], event.twilio_message_sid, event.status_callback_state
    )
    return {
        "handler": "customer_send_delivery_handler",
        "reconciled": result.matched,
        "delivery_status": result.delivery_status,
    }
