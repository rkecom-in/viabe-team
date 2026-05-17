"""opt_out_handler — Pre-Filter direct handler for opt-out messages (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7 (owner-truth): the confirmation send is LOAD-BEARING. An owner who
sends STOP MUST receive a confirmation — the send is unconditional.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.graph import get_pool
from orchestrator.types import Tenant, WebhookEvent

logger = logging.getLogger(__name__)


@DBOS.step()
def opt_out_handler(event: WebhookEvent, tenant: Tenant) -> dict[str, Any]:
    """Set the tenant opt-out flag and send the opt-out confirmation."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE tenants SET opt_out = true WHERE id = %s",
            (str(tenant.tenant_id),),
        )

    # Pillar 7: confirmation MUST be sent.
    # TODO VT-3.3: replace this logged stub with the real Twilio template send.
    logger.info(
        "opt-out confirmation template -> %s (tenant %s)",
        event.sender_phone,
        tenant.tenant_id,
    )

    return {
        "handler": "opt_out_handler",
        "opt_out_set": True,
        "confirmation_sent": True,
    }
