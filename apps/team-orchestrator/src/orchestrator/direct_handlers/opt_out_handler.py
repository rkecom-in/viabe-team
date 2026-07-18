"""opt_out_handler — Pre-Filter direct handler for opt-out messages (VT-3.8).

Pillar 1: fully deterministic, zero LLM.
Pillar 7 (owner-truth): the confirmation send is LOAD-BEARING — an owner who
sends STOP MUST receive a confirmation. The return contract reports the real
Twilio outcome (send_result), never a hardcoded send claim.

VT-384 (gate-bounce F1, the STRONG ARM): an owner opt-out is STRICTLY STRONGER
than the autonomy KILL keyword. A bare STOP / बंद करो stops sends INSTANTLY — the
honest implementation of the Meta-approved autonomy_offer promise ("say STOP to
turn this off instantly"). So this handler ALSO invokes the autonomy freeze path
(``kill_autonomy_by_keyword`` — owner_keyword regression: freeze + cancel every
open batch INCLUDING ``auto_send_pending``, same-txn), so an armed L3 hold parked
on its delivery anchor can never fire a window-expiry send over the owner's STOP.
The freeze is BEST-EFFORT and runs AFTER the opt-out write has already committed:
opt-out is the compliance priority, so if the freeze leg errors the opt-out STILL
lands (we log loudly, never crash the pipeline).
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS

from orchestrator.agents.autonomy import kill_autonomy_by_keyword
from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.direct_handlers._freeform_first import send_freeform_first

logger = logging.getLogger(__name__)


@DBOS.step()
def opt_out_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Set the tenant opt-out flag, freeze autonomy, and send the opt-out confirmation."""
    tenant_id = state["tenant_id"]
    with tenant_connection(tenant_id) as conn:
        # Compliance FIRST: the opt-out write commits on its own (autocommit pool) so it lands
        # even if the freeze leg below errors — opt-out is the binding priority (VT-384 F1).
        conn.execute(
            "UPDATE tenants SET opt_out = true WHERE id = %s",
            (str(tenant_id),),
        )

        # VT-384 F1 — the STRONG ARM. Freeze + cancel every open autonomy batch (incl.
        # auto_send_pending L3 holds) in ONE atomic txn (the VT-382 autocommit lesson:
        # multi-statement units take an explicit conn.transaction()). BEST-EFFORT: a freeze
        # failure must NEVER roll back the already-committed opt-out, so it runs in its own
        # transaction and any error is swallowed + logged (the opt-out still stands).
        autonomy_frozen = True
        try:
            with conn.transaction():
                kill_autonomy_by_keyword(tenant_id, conn=conn)
        except Exception:  # noqa: BLE001 — opt-out is the compliance priority; never block it
            autonomy_frozen = False
            logger.exception(
                "opt_out_handler: autonomy freeze FAILED (opt-out still applied) tenant=%s",
                tenant_id,
            )

    # Pillar 7: the confirmation send is unconditional; send_result is the truth.
    # VT-683 P1: the owner just sent STOP — the 24h window is open by construction, so the
    # confirmation rides the SESSION (freeform, same Fazal-approved copy) with the Meta template
    # as the transition belt (redelivery-past-window edge). Whitelist ruling 2026-07-18.
    send_result = send_freeform_first(
        tenant_id,
        "Got it. I've paused all automated messages and campaigns immediately. Your "
        "subscription remains active for billing purposes, but I won't initiate anything new "
        "until you tell me to restart. To resume, reply START. To cancel your subscription "
        "entirely, reply CANCEL and I'll process that for you. Thanks for letting me know.",
        event.sender_phone or None,
        fallback_template="team_opt_out_confirmation",
    )

    return {
        "handler": "opt_out_handler",
        "opt_out_set": True,
        "autonomy_frozen": autonomy_frozen,
        "send_result": send_result,
    }
