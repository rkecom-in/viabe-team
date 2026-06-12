"""dsr_handler — Pre-Filter direct handler for data-subject requests (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7: the acknowledgment send and the ticket creation happen within this
single @DBOS.step; the return contract reports the real Twilio outcome.

The gate detects a generic DSR keyword; it cannot deterministically classify
the request type, so the ticket is opened as 'deletion' (the DPDP default and
the dominant keyword set). VT-8 owns richer DSR classification.

VT-384 (gate-bounce F1): like opt-out, a DSR is STRICTLY STRONGER than the
autonomy KILL keyword — a data-subject request must also stop in-flight automatic
sending. So this handler ALSO invokes the autonomy freeze path
(``kill_autonomy_by_keyword`` — freeze + cancel every open batch incl.
``auto_send_pending``, same-txn). BEST-EFFORT: the ticket creation is the
compliance priority and commits first, so a freeze failure never blocks the DSR.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.agents.autonomy import kill_autonomy_by_keyword
from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_template_message

logger = logging.getLogger(__name__)


@DBOS.step()
def dsr_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Create a DSR ticket, freeze autonomy, and send the DPDP acknowledgment."""
    tenant_id = state["tenant_id"]
    with tenant_connection(tenant_id) as conn:
        # Compliance FIRST: the ticket INSERT commits on its own (autocommit pool) so it lands
        # even if the freeze leg below errors — the DSR is the binding priority (VT-384 F1).
        row = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id",
            (str(tenant_id),),
        ).fetchone()
        # The shared pool uses dict_row — access columns by name.
        ticket_id = str(row["id"]) if row else None

        # VT-384 F1 — the STRONG ARM. Freeze + cancel every open autonomy batch (incl.
        # auto_send_pending L3 holds) in ONE atomic txn (VT-382 autocommit lesson). BEST-EFFORT:
        # a freeze failure must NEVER roll back the already-committed DSR ticket, so it runs in its
        # own transaction and any error is swallowed + logged (the DSR still stands).
        autonomy_frozen = True
        try:
            with conn.transaction():
                kill_autonomy_by_keyword(tenant_id, conn=conn)
        except Exception:  # noqa: BLE001 — the DSR is the compliance priority; never block it
            autonomy_frozen = False
            logger.exception(
                "dsr_handler: autonomy freeze FAILED (DSR ticket still created) tenant=%s",
                tenant_id,
            )

    # "We received your request; we'll respond within 30 days per DPDP."
    send_result = send_template_message(
        tenant_id,
        "team_dsr_acknowledgment",
        {},
        recipient_phone=event.sender_phone or None,
    )

    return {
        "handler": "dsr_handler",
        "dsr_ticket_id": ticket_id,
        "autonomy_frozen": autonomy_frozen,
        "send_result": send_result.model_dump(),
    }
