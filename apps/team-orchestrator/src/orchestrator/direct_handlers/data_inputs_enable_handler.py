"""data_inputs_enable_handler — VT-303 enable path (the consent-grant setter).

Fires when the owner sends a deliberate enable phrase (see
config/data_inputs_enable_keywords.yaml). Sets ``tenants.owner_inputs = true`` —
the lawful basis (CL-425) that unblocks the brain transmit (gated in
runner.webhook_pipeline_run) and the peripheral owner-input surfaces (classify
VT-270, L0 writer, vision VT-52, voice VT-59). Sends a free-form confirm.

This is the consent GRANT itself — a deterministic DB write + confirmation, no
Anthropic transmit — so it is NOT itself consent-gated (gating the grant on the
grant would be a deadlock). Pillar 1: zero LLM. Pillar 7: owner authority — the
owner explicitly turns it on; STOP still turns it off (opt_out_handler).

D1a (Fazal 2026-07-12 — re-consent after opt-out): ACTIVATE TEAM is the advertised
RE-CONSENT phrase, so this handler ALSO clears ``tenants.opt_out`` (SET false). It is
now the ONLY writer that clears opt_out (symmetric to opt_out_handler, the ONLY writer
that sets it true) — the explicit re-activation IS the retraction of a prior STOP. The
send-block chokepoint (execute_approved_campaign, T13b) reads opt_out server-side, so
a send stays blocked until this handler runs; a stale pre-opt-out campaign is NOT
auto-fired by re-consent — a fresh owner ask is required. Direct handlers dispatch
UNCONDITIONALLY (runner), so ACTIVATE TEAM reaches here even while opted-out.

Without this setter, Option B's gate would degrade EVERY tenant forever (nothing
else sets owner_inputs to true yet — the web-onboarding setter is Fazal-D1
deferred). This is the minimal end-to-end enable loop.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.utils.twilio_send import send_freeform_message

_CONFIRM = (
    "Done — data inputs enabled. Your AI team is now active and will start "
    "working on recovering sales for you. Reply STOP anytime to pause."
)


@DBOS.step()
def data_inputs_enable_handler(
    event: WebhookEvent, state: SubscriberState
) -> dict[str, Any]:
    """Set tenants.owner_inputs = true AND clear opt_out (RLS-scoped) and confirm."""
    with tenant_connection(state["tenant_id"]) as conn:
        # D1a — re-consent clears BOTH the transmit grant (owner_inputs) and the send opt-out, in one
        # idempotent RLS-scoped write. This is the sole clearer of tenants.opt_out.
        conn.execute(
            "UPDATE tenants SET owner_inputs = true, opt_out = false WHERE id = %s",
            (str(state["tenant_id"]),),
        )

    sid: str | None = None
    error: str | None = None
    recipient = event.sender_phone or None
    if recipient is not None:
        try:
            # VT-611 Package H0 — thread tenant_id/surface so this confirm lands in the lifetime
            # conversation_log (was bare -> _record_owner_conversation_turn no-op'd).
            sid = send_freeform_message(
                _CONFIRM, recipient, tenant_id=state["tenant_id"], surface="system"
            )
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            error = repr(exc)
    else:
        error = "no recipient phone on event"

    return {
        "handler": "data_inputs_enable_handler",
        "owner_inputs_set": True,
        "send_result": {"sid": sid, "error": error},
    }
