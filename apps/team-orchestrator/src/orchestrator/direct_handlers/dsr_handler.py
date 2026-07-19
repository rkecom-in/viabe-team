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

VT-400 class recurrence on a DPDP surface: ``team_dsr_acknowledgment`` declares
three POSITIONAL params (owner_name, dsr_type, completion_deadline_date).
Historically this handler sent ``{}``, so Twilio rendered the template's SAMPLE
values to a REAL owner on a compliance surface (the same defect class VT-400 fixed
for the welcome). We now pass all three real values. AND — mirroring the RC1
"chat-summary-before-template" pattern — a deterministic freeform scope
confirmation is sent BEFORE the Meta template, naming what the DSR actually does
(deletion + account closure + the 30-day deadline + automation frozen now). The
owner just messaged us, so the 24h care window is open by construction; the
freeform is zero-LLM, zero new side effects (``kill_autonomy_by_keyword`` already
froze in-flight sending above).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from dbos import DBOS

from orchestrator.agents.autonomy import kill_autonomy_by_keyword
from orchestrator.db import tenant_connection
from orchestrator.state import SubscriberState
from orchestrator.types import WebhookEvent
from orchestrator.direct_handlers._freeform_first import send_freeform_first
from orchestrator.utils.twilio_send import send_freeform_message

logger = logging.getLogger(__name__)

# DPDP response deadline: the acknowledgment promises completion within 30 days.
_COMPLETION_WINDOW_DAYS = 30
# The gate cannot sub-classify the request; 'deletion' is the DPDP default (mirrors the ticket
# INSERT below). VT-8 owns richer classification — when it lands, the ticket's request_type flows
# straight through to the template's dsr_type param (read back from the row), so this constant is
# only the fallback if the INSERT ever fails to RETURN a row.
_DEFAULT_DSR_TYPE = "deletion"
# Owner-name fallback when the tenant carries no business_name (the display-name slot for the
# owner-facing template — mirrors agents.approval_glue._owner_display_name's convention).
_OWNER_NAME_FALLBACK = "there"

# --- Pillar-7 DRAFT copy — Fazal wording approval pending ---------------------------------------
# The MECHANISM (non-empty template params + a scope confirmation existing at all) is NOT contested
# — the prior behavior rendered Twilio SAMPLE values to a real owner. Only the exact EN wording of
# the scope confirmation below is Fazal's call (DPDP compliance surface); it is clearly labeled DRAFT
# and flagged for approval before the dev->main promotion. ``{deadline}`` is substituted with the
# completion_deadline_date (acknowledgment time + 30 days).
_SCOPE_CONFIRMATION_DRAFT = (
    "We've received your data request. Here's exactly what happens now:\n\n"
    "• Your personal data will be deleted and your account closed.\n"
    "• All campaigns and automation are frozen right now — nothing further will be sent.\n"
    "• We'll complete this and confirm back to you by {deadline} "
    f"(within {_COMPLETION_WINDOW_DAYS} days, as required under India's DPDP Act).\n\n"
    "You don't need to do anything else."
)


@DBOS.step()
def dsr_handler(event: WebhookEvent, state: SubscriberState) -> dict[str, Any]:
    """Create a DSR ticket, freeze autonomy, and send the DPDP acknowledgment."""
    tenant_id = state["tenant_id"]
    with tenant_connection(tenant_id) as conn:
        # Compliance FIRST: the ticket INSERT commits on its own (autocommit pool) so it lands
        # even if the freeze leg below errors — the DSR is the binding priority (VT-384 F1).
        row = conn.execute(
            "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
            "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id, request_type",
            (str(tenant_id),),
        ).fetchone()
        # The shared pool uses dict_row — access columns by name.
        ticket_id = str(row["id"]) if row else None
        # dsr_type comes FROM THE TICKET (VT-8-ready): whatever request_type the row carries flows to
        # the template, so a future richer classification needs no change here.
        dsr_type = row["request_type"] if row else _DEFAULT_DSR_TYPE

        # Owner display name for the acknowledgment template (VT-400 fix — a real value, never the
        # Twilio SAMPLE). tenants has no dedicated owner_name column; business_name IS the owner
        # display name (agents.approval_glue._owner_display_name convention).
        name_row = conn.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (str(tenant_id),)
        ).fetchone()
        owner_name = (
            name_row["business_name"] if name_row and name_row["business_name"] else _OWNER_NAME_FALLBACK
        )

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

    # completion_deadline_date = acknowledgment time + 30 days. Computed once so the freeform scope
    # confirmation and the template ack name the SAME date.
    deadline_str = (datetime.now(UTC) + timedelta(days=_COMPLETION_WINDOW_DAYS)).strftime("%d %B %Y")
    recipient = event.sender_phone or None

    # RC1 pattern — the deterministic scope confirmation BEFORE the Meta template. In-window by
    # construction (the owner just messaged); zero LLM. BEST-EFFORT: an honest send outcome, never a
    # crash of the compliance pipeline (the ticket + freeze already committed above).
    scope_sid: str | None = None
    scope_error: str | None = None
    if recipient is not None:
        try:
            scope_sid = send_freeform_message(
                _SCOPE_CONFIRMATION_DRAFT.format(deadline=deadline_str),
                recipient,
                tenant_id=tenant_id,
                surface="system",
            )
        except Exception as exc:  # noqa: BLE001 — honest send outcome, never crash the pipeline
            scope_error = repr(exc)
    else:
        scope_error = "no recipient phone on event"

    # "We received your request; we'll respond within 30 days per DPDP." VT-683 P1: rides the
    # SESSION (the owner just messaged — window open by construction) with the Meta template as
    # the transition belt (same three real params on fallback, VT-400). Copy = same DRAFT
    # convention as _SCOPE_CONFIRMATION_DRAFT (Fazal wording approval pending).
    send_result = send_freeform_first(
        tenant_id,
        f"Hi {owner_name}, we've received your {dsr_type} request and will complete it within "
        f"{_COMPLETION_WINDOW_DAYS} days — by {deadline_str}. Automated messaging stays paused "
        "in the meantime.",
        recipient,
        fallback_template="team_dsr_acknowledgment",
        fallback_params={
            "owner_name": owner_name,
            "dsr_type": dsr_type,
            "completion_deadline_date": deadline_str,
        },
    )

    return {
        "handler": "dsr_handler",
        "dsr_ticket_id": ticket_id,
        "autonomy_frozen": autonomy_frozen,
        "scope_confirmation": {"sid": scope_sid, "error": scope_error},
        "send_result": send_result,
    }
