"""dupe_handler — Pre-Filter direct handler for duplicate deliveries (VT-3.8).

Pillar 1: fully deterministic, zero LLM.

Duplicate detection itself is native to DBOS (workflow_id idempotency); this
handler just confirms the duplicate was caught and ends the workflow cleanly.
The Pre-Filter Gate does not route here on its own — VT-3.3 wires the DBOS
workflow layer to invoke it when a workflow_id has already executed.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.types import Tenant, WebhookEvent

logger = logging.getLogger(__name__)


@DBOS.step()
def dupe_handler(event: WebhookEvent, tenant: Tenant) -> dict[str, Any]:
    """Confirm a duplicate webhook delivery and end the workflow."""
    logger.info(
        "duplicate webhook delivery confirmed (sid=%s, tenant=%s)",
        event.twilio_message_sid,
        tenant.tenant_id,
    )
    return {"handler": "dupe_handler", "duplicate_confirmed": True}
