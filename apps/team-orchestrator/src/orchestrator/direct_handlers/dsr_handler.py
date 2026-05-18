"""dsr_handler — Pre-Filter direct handler for data-subject requests (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7: the acknowledgment send and the ticket creation happen within this
single @DBOS.step; the return contract reports the real Twilio outcome.

The gate detects a generic DSR keyword; it cannot deterministically classify
the request type, so the ticket is opened as 'deletion' (the DPDP default and
the dominant keyword set). VT-8 owns richer DSR classification.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_template_message


@DBOS.step()
def dsr_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Create a DSR ticket and send the DPDP acknowledgment."""
    with tenant_connection(state["tenant_id"]) as conn:
        row = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id",
            (str(state["tenant_id"]),),
        ).fetchone()
    # The shared pool uses dict_row — access columns by name.
    ticket_id = str(row["id"]) if row else None

    # "We received your request; we'll respond within 30 days per DPDP."
    send_result = send_template_message(
        state["tenant_id"],
        "team_dsr_acknowledgment",
        {},
        recipient_phone=event.sender_phone or None,
    )

    return {
        "handler": "dsr_handler",
        "dsr_ticket_id": ticket_id,
        "send_result": send_result.model_dump(),
    }
