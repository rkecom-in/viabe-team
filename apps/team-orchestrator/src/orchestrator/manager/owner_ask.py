"""VT-755 scopes 0 + 1 — the emitter for a pending question, and the gate on what may reach an owner.

TWO DEFECTS, ONE FIX, because D-A settled them together.

**Scope 0 — `ask()` had no delivery path.** `manager_review`'s `ask_owner` branch wrote a row into
`pending_questions`, parked the task `waiting_owner`, and the durable loop then waited for an answer
to a question **the owner had never been sent**. A park waiting on an undelivered question is a
guaranteed stall: it can only end at the poll ceiling, hours later, with the owner never having been
given the chance to say anything.

**Scope 1 — the owner-facing text was raw model prose.** `review._insufficient_data` built:

    "I couldn't build the win-back campaign yet because some information is missing.
     Could you help with this: {'; '.join(item.suggested_remediation …)}?"

`suggested_remediation` is `str = Field(..., min_length=1)` on `MissingDataItem` — free text the
model writes for an ENGINEERING audience. It is where "backfill the customer table" reaches a shop
owner in Hinglish.

RULING D-A (Fazal 2026-08-15, CL-2026-08-15-three-m2b-rulings): **fail-closed; raw model remediation
never reaches an owner; not-from-closed-vocabulary → an honest "I need X from you", emitted through
the SINGLE choke.**

HOW THE CLOSED VOCABULARY IS DECIDED, AND WHY IT IS NOT A KEYWORD LIST. The row's earlier analysis
looked for a structural signal on the model's output and found none (`MissingDataItem.category` is
free text too), leaving "an LLM judgment" or "escalate on anything unrecognised". Both treat the
MODEL's output as the thing to classify. That is the wrong source: the model's account of what is
missing is precisely what may not reach the owner.

So this module does not classify the model's prose at all. It asks the tenant's OWN STATE what is
missing — is a data source connected, are there customers, is there purchase history — and states
that. Those are three deterministic DB facts, each with one sentence written by us. The result is a
genuinely closed vocabulary, grounded in something checkable, and the model's prose is not consulted
in composing owner copy even once. The model still decides THAT it is blocked; it no longer gets to
tell the owner WHY in its own words.

The needs ladder is ordered because the answers nest: no source connected makes "no customers"
uninformative, and no customers makes "no purchase history" uninformative. The owner is asked for the
one thing that unblocks the next question, never a list.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = ["compose_owner_need", "deliver_pending_question"]


# The CLOSED vocabulary. One sentence per verifiable state of the tenant's own data — never a
# rendering of anything the model wrote. Each says what is missing AND what the owner can do, because
# an ask that does not name an action is just a nicer way of stalling.
_NEED_NO_SOURCE = (
    "I can't build this yet — your customer data isn't connected on my side, so I have nothing to "
    "work from. Connect a source (Shopify or a Google Sheet) and I'll pick this up from here."
)
_NEED_NO_CUSTOMERS = (
    "I can't build this yet — your data source is connected, but no customers have come through "
    "from it. If you expect some to be there, tell me and I'll check the sync."
)
_NEED_NO_HISTORY = (
    "I can't build this yet — I have your customers, but no purchase history for them, so I can't "
    "tell who's gone quiet. If your sales history lives somewhere else, tell me where and I'll "
    "bring it in."
)
_NEED_UNKNOWN = (
    "I can't build this yet — something I need from your data is missing, and I'd rather ask than "
    "guess. Can you tell me what customer and sales data you have, and where it lives?"
)


def compose_owner_need(tenant_id: UUID | str) -> str:
    """The owner-facing ask, composed from the tenant's own state (D-A fail-closed).

    Ordered because the answers nest — an unconnected source makes "no customers" uninformative.
    Any read failure falls to ``_NEED_UNKNOWN``, which is honest without asserting anything: the one
    thing that must never happen here is a confident claim about the owner's data that we could not
    actually verify.
    """
    try:
        from orchestrator.db.wrappers import CustomersWrapper
        from orchestrator.integrations.connection_truth import customer_data_source_connected

        if not customer_data_source_connected(tenant_id):
            return _NEED_NO_SOURCE
        cw = CustomersWrapper()
        if cw.count_all(tenant_id) == 0:
            return _NEED_NO_CUSTOMERS
        has_base, _n = _lapsed_base(cw, tenant_id)
        if not has_base:
            return _NEED_NO_HISTORY
    except Exception:  # noqa: BLE001 — an unverifiable state gets the honest unknown, never a guess
        logger.warning("VT-755: owner-need state read failed tenant=%s — asking generically", tenant_id)
        return _NEED_UNKNOWN
    # Source connected, customers present, history present — whatever the specialist found missing is
    # not one of the three things an owner can hand us. Ask rather than assert.
    return _NEED_UNKNOWN


def _lapsed_base(cw: Any, tenant_id: UUID | str) -> tuple[bool, int]:
    """Does a purchase base exist at all? Reuses `status_query`'s own helper so "has sales history"
    means the SAME thing in the ask as it does in every answer the owner has already been given."""
    from orchestrator.owner_inputs.status_query import _lapsed_stats

    return _lapsed_stats(cw, tenant_id)


def deliver_pending_question(
    tenant_id: UUID | str, question_id: UUID | str, body: str
) -> bool:
    """Send the question through the SINGLE owner-emission choke and stamp `delivered_at`.

    Returns True only when the send actually succeeded AND the stamp landed. The caller must treat
    False as "the owner has not been asked" — because parking `waiting_owner` on an undelivered
    question is the stall this row exists to close.

    No second send path: this goes through `send_freeform_ack` → `twilio_send`, the same funnel every
    other Manager utterance uses, so the Manager stays one voice (NORTH-STAR 2026-07-28) and the
    turn is recorded in the lifetime conversation log by the transport chokepoint.
    """
    recipient = _owner_phone(tenant_id)
    if not recipient:
        logger.warning("VT-755: no owner phone for tenant=%s — question %s undelivered", tenant_id, question_id)
        return False
    try:
        from orchestrator.owner_surface.freeform_acks import send_freeform_ack

        if not send_freeform_ack(tenant_id, recipient, body):
            return False
        from orchestrator.manager import pending_questions

        pending_questions.mark_delivered(tenant_id, question_id)
        logger.info("VT-755: pending question %s delivered tenant=%s", question_id, tenant_id)
        return True
    except Exception:  # noqa: BLE001 — an undelivered question is a handled outcome, never a crash
        logger.warning(
            "VT-755: delivery failed for question %s tenant=%s", question_id, tenant_id, exc_info=True
        )
        return False


def _owner_phone(tenant_id: UUID | str) -> str | None:
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s", (str(tenant_id),)
            ).fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get("owner_phone") or row.get("whatsapp_number")
        return row[0] or row[1]
    except Exception:  # noqa: BLE001
        return None
