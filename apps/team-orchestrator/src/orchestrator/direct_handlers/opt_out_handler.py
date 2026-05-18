"""opt_out_handler — Pre-Filter direct handler for opt-out messages (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7 (owner-truth): the confirmation send is LOAD-BEARING — an owner who
sends STOP MUST receive a confirmation. The return contract reports the real
Twilio outcome (send_result), never a hardcoded send claim.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.graph import get_pool
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_template_message


@DBOS.step()
def opt_out_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Set the tenant opt-out flag and send the opt-out confirmation."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE tenants SET opt_out = true WHERE id = %s",
            (str(state["tenant_id"]),),
        )

    # Pillar 7: the confirmation send is unconditional; send_result is the truth.
    send_result = send_template_message(
        state["tenant_id"],
        "team_opt_out_confirmation",
        {},
        recipient_phone=event.sender_phone or None,
    )

    return {
        "handler": "opt_out_handler",
        "opt_out_set": True,
        "send_result": send_result.model_dump(),
    }
