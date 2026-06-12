"""autonomy_kill_handler — VT-384 (Gap-5 PR-3) L3 KILL keyword path.

Fires when the owner sends an AUTONOMY-SPECIFIC kill phrase ("turn off automatic sending" /
"disable auto sending" / "ऑटोमैटिक बंद" — config/l3_keywords.yaml, lockstep with the CL-438 offer's
"you can always say STOP" promise). This is NARROWER than a DPDP opt-out: bare STOP / बंद करो is the
AUTHORITATIVE opt-out and is routed to opt_out_handler FIRST (the CL-438 floor + the pre_filter
RULE_ORDER pin), so any phrase carrying a bare opt-out keyword ("stop automatic sending", "auto band
karo") is caught by opt-out and never reaches here — those are DELIBERATELY absent from the kill set.
This handler covers "turn off only the automatic sending" without a full opt-out.

The kill records the ``owner_keyword`` regression for EVERY owning agent of the tenant — the
substrate FREEZE path, which ATOMICALLY cancels in-flight L3 holds + batches in the SAME
transaction (``_OPEN_BATCH_STATUSES`` includes ``auto_send_pending``/``sending``). So a hold parked
on its delivery anchor is killed the instant the keyword lands — a window-expiry send can never
fire over the owner's objection (the original race requirement).

Pillar 1: fully deterministic, zero LLM. Pillar 7: owner authority is absolute — the kill is
unconditional and atomic. CL-390: log agent counts only — never the owner phone/body.

Registered in ``direct_handlers/__init__.py`` HANDLERS as ``"autonomy_kill_handler"`` (the name
pre_filter rule b2 routes to); runner.py:webhook_pipeline_run dispatches it via HANDLERS[name].
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.agents.autonomy import kill_autonomy_by_keyword
from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_freeform_message

logger = logging.getLogger(__name__)

_CONFIRM = (
    "Done — automatic sending is off. Your Viabe assistant will go back to asking your approval "
    "before every customer message. Reply STOP anytime to pause all messages."
)


@DBOS.step()
def autonomy_kill_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Freeze + cancel all in-flight autonomy for the tenant (one txn), then confirm."""
    tenant_id = state["tenant_id"]
    with tenant_connection(tenant_id) as conn, conn.transaction():
        per_agent = kill_autonomy_by_keyword(tenant_id, conn=conn)

    agents_killed = sum(1 for v in per_agent.values() if v)
    logger.info(
        "autonomy_kill_handler: tenant=%s agents_frozen=%d", tenant_id, agents_killed
    )

    sid: str | None = None
    send_error: str | None = None
    recipient = event.sender_phone or None
    if recipient is not None:
        try:
            sid = send_freeform_message(_CONFIRM, recipient)
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            send_error = repr(exc)
    else:
        send_error = "no recipient phone on event"

    return {
        "handler": "autonomy_kill_handler",
        "autonomy_killed": True,
        "agents_frozen": agents_killed,
        "send_result": {"sid": sid, "error": send_error},
    }
