"""status_ping_handler — Pre-Filter direct handler for status pings (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7: report ACCURATE state only — no padding, no overstatement; the
return contract reports the real Twilio send outcome.

NOTE: "last campaign / next scheduled action" are not yet in the Phase-1
schema. Per Pillar 7 this handler reports only the tenant state that exists
(lifecycle phase); richer state reporting lands when VT-3.2 (SubscriberState)
and the campaign tables ship.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.graph import get_pool
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_template_message


@DBOS.step()
def status_ping_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Reply to a status ping with the tenant's current, accurate state."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT business_name, phase, phase_entered_at FROM tenants WHERE id = %s",
            (str(state["tenant_id"]),),
        ).fetchone()

    if row is None:
        status_text = "No account state on file."
    else:
        # The shared pool uses dict_row — access columns by name.
        phase_entered_at = row["phase_entered_at"]
        since = f" since {phase_entered_at:%Y-%m-%d}" if phase_entered_at else ""
        status_text = (
            f"{row['business_name']}: current phase '{row['phase']}'{since}."
        )

    send_result = send_template_message(
        state["tenant_id"],
        "team_status_ping",
        {},
        recipient_phone=event.sender_phone or None,
    )

    return {
        "handler": "status_ping_handler",
        "status_text": status_text,
        "send_result": send_result.model_dump(),
    }
