"""VT-742 §1 — the ONE function that answers "which number does this tenant send from".

## Why this module exists

`tenant_whatsapp_accounts.phone_number` has been populated per tenant since VT-286 (the Embedded
Signup exchange provisions a dedicated number and writes it), and **every send ignored it**: the
three `messages.create` sites in `utils/twilio_send.py` each read
`os.environ["TEAM_TWILIO_FROM_NUMBER"]`, a single process-wide constant. A tenant who completed
onboarding still sent from the shared Viabe number. Measured on dev 2026-08-14: **138 of 139 WABA
rows `live`, all 138 with a number, and not one of them used.**

## The reframe (VT-742 pre-build investigation, and the reason this is not merely hygiene)

Customer INBOUND already resolves by the number the customer messaged **TO**:

```sql
SELECT tenant_id FROM tenant_whatsapp_accounts WHERE phone_number = %s AND status = 'live'
```

So when we send FROM the shared number, the customer's reply arrives with `To` = the shared number,
which matches no `tenant_whatsapp_accounts` row — and the inbound resolves to **no tenant**. Under a
shared sender **a customer can be messaged but cannot be heard.** The two halves of the customer
conversation were built against different senders. That is why a customer send here REFUSES rather
than falling back to shared (``audience="customer"``): sending a message the customer cannot answer
is worse than not sending it, and this row is allowed to suppress a send but never to authorise one.

## The AUDIENCE split — the correction that came out of building this

The row says "replace all three env reads with this call", which read literally puts every send on the
tenant's own WABA. **That is wrong for OWNER-facing sends, and it would have been a silent product
change.** The tenant's WABA is *their shop's* number, displaying *their* name. The Manager messaging
the owner from it means the owner receives Viabe's messages from their own business identity, and the
Viabe relationship disappears from the surface where it lives. That is Fazal's call to make, not a
side effect of a refactor — so owner sends keep coming from the Viabe estate, byte-identically to
today, and only customer-facing sends move to the tenant's own number. Flagged for his ruling.

(Owner INBOUND routing is unaffected either way: `_lookup_tenant` matches the owner's `From` against
`tenants.whatsapp_number` and runs BEFORE the customer lookup. This split is about identity, not
routing.)

## Precedence (VT-742 §1), evaluated in order

**`audience="customer"`** — a send to an end customer:
1. the tenant's **own live WABA number**, REQUIRED — VT-286's output, finally consumed. No own live
   WABA (or a malformed one) → **refuse**. There is no acceptable downgrade: see the reframe above.

**`audience="owner"`** — a send to the business owner (the default):
1. the tenant's **pinned shared number** (`tenants.pinned_sender_e164`, migration 207)
2. else the **default shared number** (`TEAM_TWILIO_FROM_NUMBER`)

The owner path never reads `tenant_whatsapp_accounts` at all. Pinning still applies to it, because
owner traffic counts toward the reputation of whichever shared number it leaves from — which is the
entire point of pinning.

**PIN, never round-robin** (VT-742 §2, decided before a second number exists): a tenant is assigned
one number and stays on it, so one bad tenant's reputation damage is contained to the tenants sharing
that number instead of being spread across the whole estate. Today the estate is one number, so step
2 never fires in practice — it is built now because it is cheap now and a migration later, and gate
(g) requires the precedence be provable with a single number.

## Fail-closed

`SenderUnresolvable` when no step yields a well-formed E.164 number, or when a customer send has no
own live WABA. **Never fall through to "some number"** — a wrong `from_` is a cross-tenant identity
error, not a cosmetic one.

A customer send with **no tenant** is also refused. The production customer-inbound path
(`customer_inbound._default_send`) was calling the transport with `is_customer_session=True` and no
tenant at all — the pre-push suite caught it. The fix is to thread the tenant, which
`handle_customer_inbound` has all along, not to allow a customer send that cannot name whose customer
it is.

## What this module must NOT touch

`tenants.whatsapp_number` is the **owner-inbound identity key** (globally unique, mig 066/VT-267,
matched against an inbound's `From`) and simultaneously the owner-facing notification RECIPIENT
(`twilio_send.get_tenant_whatsapp_number`). Migration 050's comment calling it "the WABA sender" is
STALE — nothing in `src/` writes it. Reading it here would conflate an owner routing key with a
customer-facing sender identity; the sender lives in `tenant_whatsapp_accounts` and nowhere else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.utils.phone_e164 import is_e164

logger = logging.getLogger(__name__)

#: Optional CSV of every shared number we own, for when the estate grows past one. The default
#: sender is always a member whether or not it is listed. Consumed by ``shared_sender_numbers``
#: (the inbound guard) and by pin validation — both need to know "is this number a shared asset".
_SHARED_ESTATE_ENV = "TEAM_TWILIO_SHARED_SENDER_NUMBERS"
_DEFAULT_SENDER_ENV = "TEAM_TWILIO_FROM_NUMBER"

#: ``kind`` values. The provenance of a sender is auditable from the Sender itself — a log line or
#: an alert can say WHICH rule produced the number without re-deriving it.
KIND_OWN_WABA = "own_waba"
KIND_PINNED_SHARED = "pinned_shared"
KIND_DEFAULT_SHARED = "default_shared"


class SenderUnresolvable(Exception):
    """No well-formed sending number could be resolved — the send MUST NOT happen.

    Distinct from ``twilio_send.BlockedRecipientError`` (a bad RECIPIENT). Raised when the estate
    is misconfigured (no default sender in the environment), when a stored number is malformed, or
    when a customer send has no own live WABA to send from.
    """


@dataclass(frozen=True, slots=True)
class Sender:
    """A resolved sending identity. ``kind`` names which precedence rule produced it."""

    phone_number: str
    kind: str
    tenant_id: str | None = None

    @property
    def is_shared(self) -> bool:
        return self.kind in (KIND_PINNED_SHARED, KIND_DEFAULT_SHARED)


def default_shared_sender() -> str | None:
    """The process-wide shared sender, or None when unset/blank. Never raises."""
    value = (os.environ.get(_DEFAULT_SENDER_ENV) or "").strip()
    return value or None


def shared_sender_numbers() -> frozenset[str]:
    """Every number known to be a SHARED asset (the default sender + the optional estate CSV).

    The inbound guard uses this to refuse resolving a customer inbound addressed to a shared
    number. A shared number must never appear in ``tenant_whatsapp_accounts.phone_number``: with
        `... WHERE phone_number = %s AND status='live' LIMIT 1`
    a single such row would route EVERY tenant's customer replies to that one tenant. Migration 207
    makes it structurally hard (partial UNIQUE) and the guard makes it impossible to act on.
    """
    numbers = {n for n in (default_shared_sender(),) if n}
    for raw in (os.environ.get(_SHARED_ESTATE_ENV) or "").split(","):
        candidate = raw.strip()
        if candidate:
            numbers.add(candidate)
    return frozenset(numbers)


_SENDER_QUERY = """
    SELECT wa.phone_number AS waba_number,
           wa.status       AS waba_status,
           t.pinned_sender_e164
      FROM tenants t
      LEFT JOIN tenant_whatsapp_accounts wa ON wa.tenant_id = t.id
     WHERE t.id = %s
"""


def _read_tenant_sender_state(tenant_id: UUID | str, conn: Any) -> dict[str, Any] | None:
    """One round trip for both precedence sources. ``conn`` may be an already-open RLS-scoped
    tenant connection (mirrors ``wa_send_allowed``'s VT-460 contract — a caller inside a
    ``tenant_connection`` must not open a second pool connection); None opens its own."""
    params = (str(tenant_id),)
    if conn is not None:
        row = conn.execute(_SENDER_QUERY, params).fetchone()
    else:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as own_conn, own_conn.cursor() as cur:
            cur.execute(_SENDER_QUERY, params)
            row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {"waba_number": row[0], "waba_status": row[1], "pinned_sender_e164": row[2]}


AUDIENCE_OWNER = "owner"
AUDIENCE_CUSTOMER = "customer"


def resolve_sender(
    tenant_id: UUID | str | None,
    *,
    audience: str = AUDIENCE_OWNER,
    conn: Any = None,
) -> Sender:
    """Resolve the number this tenant sends from. See the module docstring for precedence.

    ``audience="customer"`` (every end-customer send) resolves ONLY the tenant's own live WABA and
    refuses otherwise — a customer messaged from a shared number cannot reply to us at all. It
    requires a tenant: a customer send that cannot name whose customer it is has no sender.

    ``audience="owner"`` (the default) resolves within the shared Viabe estate — pin, else default —
    and never reads the tenant's WABA. ``tenant_id=None`` is legal here and yields the default: some
    owner-facing helpers genuinely have no tenant in hand (they are addressed by phone).

    Raises ``SenderUnresolvable`` rather than returning a fallback — a send with the wrong ``from_``
    is a cross-tenant identity error.
    """
    if audience not in (AUDIENCE_OWNER, AUDIENCE_CUSTOMER):
        raise SenderUnresolvable(
            f"unknown sender audience {audience!r}; expected {AUDIENCE_OWNER!r} or "
            f"{AUDIENCE_CUSTOMER!r}. Fail-closed rather than guessing whose identity to send as."
        )
    tid = str(tenant_id) if tenant_id is not None else None

    if audience == AUDIENCE_CUSTOMER and tid is None:
        raise SenderUnresolvable(
            "a customer send needs a tenant_id — it has no WABA to resolve otherwise, and it must "
            "not fall back to a shared number the customer cannot reply to"
        )

    if audience == AUDIENCE_CUSTOMER:
        state = _read_tenant_sender_state(tid, conn)
        if state is None:
            # No visible tenant row (absent, or RLS-invisible on this connection) — terminal for a
            # customer send.
            logger.warning("resolve_sender: no tenants row visible for tenant=%s", tid)
            state = {}
        waba_number = state.get("waba_number")
        if state.get("waba_status") != "live" or not waba_number:
            raise SenderUnresolvable(
                f"customer send for tenant {tid} requires the tenant's OWN live WABA sender "
                f"(waba_status={state.get('waba_status')!r}, number_present="
                f"{bool(waba_number)}). Sending from a shared number produces a message the "
                "customer cannot reply to — VT-742 finding 2."
            )
        if not is_e164(str(waba_number)):
            # Never dispatched, and never silently downgraded either: falling through to shared
            # would look like a tenant who never onboarded, which is how VT-286's output stayed
            # invisible for two months.
            logger.error(
                "resolve_sender: tenant=%s has a LIVE WABA whose phone_number is not E.164 — "
                "refusing it as a sender (len=%d). Fix the row.",
                tid,
                len(str(waba_number)),
            )
            raise SenderUnresolvable(
                f"tenant {tid} has a live WABA whose phone_number is not well-formed E.164; "
                "a customer send has no sender it could be answered on"
            )
        return Sender(str(waba_number), KIND_OWN_WABA, tid)

    # --- owner audience: the shared estate only -------------------------------------------
    #
    # The pin is read ONLY when the estate holds more than one number. With a single shared number a
    # pin can only point at that same number, so the read cannot change the answer — and paying a
    # per-send tenant query to confirm a column that is NULL everywhere until a second number is
    # bought is cost for nothing. Declaring the second number in
    # TEAM_TWILIO_SHARED_SENDER_NUMBERS activates the read; no code change at that moment.
    # This also keeps every owner send byte-identical to pre-VT-742 behaviour: no DB read, no
    # substrate dependency, same number out.
    if tid is not None and len(shared_sender_numbers()) > 1:
        state = _read_tenant_sender_state(tid, conn) or {}
        pinned = state.get("pinned_sender_e164")
        if pinned:
            if is_e164(str(pinned)):
                return Sender(str(pinned), KIND_PINNED_SHARED, tid)
            logger.error(
                "resolve_sender: tenant=%s pinned_sender_e164 is not E.164 — ignoring the pin "
                "(len=%d)",
                tid,
                len(str(pinned)),
            )

    default = default_shared_sender()
    if default and is_e164(default):
        return Sender(default, KIND_DEFAULT_SHARED, tid)
    raise SenderUnresolvable(
        f"no sending number resolved for tenant={tid}: no valid pin, and {_DEFAULT_SENDER_ENV} is "
        + ("malformed" if default else "unset")
        + ". Fail-closed — no send."
    )


__all__ = [
    "AUDIENCE_CUSTOMER",
    "AUDIENCE_OWNER",
    "KIND_DEFAULT_SHARED",
    "KIND_OWN_WABA",
    "KIND_PINNED_SHARED",
    "Sender",
    "SenderUnresolvable",
    "default_shared_sender",
    "resolve_sender",
    "shared_sender_numbers",
]
