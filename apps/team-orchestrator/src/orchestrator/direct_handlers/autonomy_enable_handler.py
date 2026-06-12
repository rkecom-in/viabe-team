"""autonomy_enable_handler — VT-384 (Gap-5 PR-3) L3 ENABLE path (the grant setter).

Fires when the owner sends the deliberate ENABLE verb the ``team_autonomy_offer`` promised (see
config/l3_keywords.yaml, lockstep with the CL-438 approved offer body). Resolves the open
``autonomy_upgrade`` approval (the C3 consent-evidence row) and GRANTS L3 for the agent the offer
was armed for — atomically, in ONE transaction.

Pillar 1: fully deterministic, zero LLM (the pre_filter ENABLE rule is an exact whole-body match;
this handler does a DB resolve + grant, no reasoning). Pillar 7: owner authority — the owner
explicitly opts in; the durable approval row is the consent record (CL-438 C3). ``grant_l3``
RE-VALIDATES the streak/frozen/level in-txn, so a stale ENABLE (streak since broken) no-ops.

CL-390: log approval_id + agent + tenant_id ONLY — never the owner phone/body.

Registered in ``direct_handlers/__init__.py`` HANDLERS as ``"autonomy_enable_handler"`` (the name
pre_filter rule b3 routes to); runner.py:webhook_pipeline_run dispatches it via HANDLERS[name].
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.agents.autonomy import find_open_autonomy_upgrade, resolve_and_grant_l3
from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_freeform_message

logger = logging.getLogger(__name__)

_CONFIRM = (
    "Done — automatic sending is on. Your Viabe assistant will send routine customer messages on "
    "its own, and you'll get a notice 2 hours before each one. Reply STOP anytime to turn it off."
)
_NOOP = (
    "There's no automatic-sending offer to turn on right now. You'll get an offer once your "
    "assistant has a steady run of approvals. Reply STOP anytime to pause messages."
)


@DBOS.step()
def autonomy_enable_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Resolve the open autonomy_upgrade approval + grant L3 (RLS-scoped, one txn), then confirm."""
    tenant_id = state["tenant_id"]
    granted_agent: str | None = None
    level: str | None = None
    with tenant_connection(tenant_id) as conn, conn.transaction():
        open_upgrade = find_open_autonomy_upgrade(tenant_id, conn=conn)
        if open_upgrade is not None:
            granted_agent, new_state = resolve_and_grant_l3(
                tenant_id, open_upgrade["id"], conn=conn
            )
            level = new_state.level if new_state is not None else None

    granted = granted_agent is not None and level == "L3"
    logger.info(
        "autonomy_enable_handler: tenant=%s agent=%s granted=%s level=%s",
        tenant_id, granted_agent, granted, level,
    )

    sid: str | None = None
    send_error: str | None = None
    recipient = event.sender_phone or None
    if recipient is not None:
        try:
            sid = send_freeform_message(_CONFIRM if granted else _NOOP, recipient)
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            send_error = repr(exc)
    else:
        send_error = "no recipient phone on event"

    return {
        "handler": "autonomy_enable_handler",
        "l3_granted": granted,
        "agent": granted_agent,
        "send_result": {"sid": sid, "error": send_error},
    }
