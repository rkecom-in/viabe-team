"""VT-740 — per-recipient send suppression. The mechanism; VT-741 supplies the number.

WHAT THIS DEFENDS AGAINST
-------------------------
At least three paths automatically re-drive a manager_task, and none of them asks what already went
out: the hourly reaper wake (`orphan_reaper`, effect-blind, and the same code is on `main`),
`approval_resume.redrive_task` when an owner replies yes, and an operator redrive. A task whose
campaign messaged 40 of 100 customers and then died is re-driven on exactly the same terms as one
that sent nothing.

Send idempotency does not cover it: `idempotency_key = f"agent:{draft_id}"` is keyed to the DRAFT,
and a re-drive re-runs the specialist, which mints new drafts and therefore new keys.

WHY IT LIVES AT THE SEND CHOKE AND NOT ON THE RE-DRIVE PATHS
------------------------------------------------------------
Gating the re-drive paths one at a time means getting all of them right, forever, including the
ones nobody has thought of. A guard at the send chokepoint is path-independent: it does not care
WHY a second send was attempted, only that this customer already received one. It also needs no
task->campaign attribution, which is the thing that does not currently work
(`campaign_messages.campaign_id` is never populated).

THE (a)/(b) LINE — carried from Fazal's 2026-08-10 direction, deliberately, into the code
-----------------------------------------------------------------------------------------
**(a) "How often should THIS business contact this customer?"** is a genuine product/policy call.
It is tenant-specific, it may one day be reasoned by the Manager rather than tabulated, and it is
what `resolve_interval_hours` below is the socket for.

**(b) "Should this customer receive the same message twice because a process crashed?"** is NOT a
policy question. There is no tenant for whom the answer is yes, and asking the Manager to have an
opinion about a bug is a category error. That half is this module's fixed behaviour and must never
become configurable.

RELATIONSHIP TO THE AGENT-CONTACT CAPS (Clau's audit question, answered here)
-----------------------------------------------------------------------------
`agents/customer_send.py` already carries `RECONTACT_SUPPRESSION_DAYS = 30`,
`MAX_AGENT_CONTACTS_PER_90D = 2`, `AGENT_SEND_CUSTOMER_WEEKLY_CAP` and a tenant daily cap. **Those
are NOT redundant and are NOT retired by this module.** They answer a different question and read a
different table:

- **The agent caps** ask *"how often may an AGENT cold-contact this customer?"* They read
  `agent_customer_contacts` and bind the agent draft-send path only. They are the stricter,
  narrower bar — a 2-per-90-days ceiling on unsolicited outreach.
- **This module** asks *"has this customer been delivered ANY message recently?"* It reads the send
  ledger and binds every customer send, campaign fan-out included.

**Precedence is deterministic without needing a rule, because both are VETO-ONLY.** Neither can
authorize a send; each can only stop one. Two conjunctive vetoes compose to "most restrictive
wins" by construction, and the outcome is identical whichever runs first — so call order is not a
tie-break that could drift. That is the property to preserve: if either layer is ever given a
branch that PERMITS a send, this ceases to be true and the two become a genuine conflict.

SUPPRESSION ONLY, NEVER AUTHORIZATION
-------------------------------------
Nothing here can permit a send. Consent, opt-out, complaint-freeze, onboarded/activation,
ownership and Pillar-7 all sit UPSTREAM and are untouched. Every failure mode in this module
suppresses harder, never softer: an unreadable ledger, a missing customer, a DB error and an empty
history all resolve to the most conservative interval.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger("orchestrator.agents.send_frequency")

#: The fail-closed interval, in hours. This is NOT a number invented here: Fazal ratified the
#: frequency rule on 2026-08-10 with Tier C ("everyone else", 7 days) as the explicit fail-closed
#: floor — "error, partial read, missing data, zero history -> Tier C". VT-741 adds Tier A (24h)
#: and Tier B (3 days) on top; until it lands, every customer is treated as Tier C, which is the
#: safe direction (it suppresses more, never less).
FAIL_CLOSED_INTERVAL_HOURS = 7 * 24

#: `send_idempotency_keys.send_status` values that mean a message actually REACHED the customer.
#: The others ('window_closed', 'rate_limited', 'error') are recorded ATTEMPTS that did not
#: deliver — counting them would suppress a customer who never heard from us.
_DELIVERED = ("sent",)


def resolve_interval_hours(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None
) -> int:
    """The minimum hours that must pass before this customer may be messaged again.

    **This is the socket.** Today it returns the ratified fail-closed interval for everyone. VT-741
    replaces the body with the ordered tier rule (A 24h / B 3 days / C 7 days, first match wins),
    and if the Manager ever earns the call, it replaces it again — the enforcement below does not
    change either time. Building the socket is what makes that argument cheap: nothing downstream
    has to move when the number's source does.

    Whatever supplies it, two properties are structural and not negotiable: it can only ever
    SUPPRESS (there is no return value that increases send rate), and any failure to determine a
    tier resolves to the most conservative interval rather than the most permissive.
    """
    return FAIL_CLOSED_INTERVAL_HOURS


def recent_delivery_within(
    tenant_id: UUID | str,
    customer_id: UUID | str,
    *,
    hours: int,
    conn: Any,
) -> bool | None:
    """Has this customer been DELIVERED a message in the last ``hours``?

    Returns True/False, or **None when the question could not be answered** — which the caller must
    treat as True. Returning False on an error would turn a database blip into a duplicate message
    to a real person, which is precisely the failure this module exists to prevent.

    Reads ``send_idempotency_keys``, which carries ``customer_id`` and is indexed on
    ``(tenant_id, customer_id, created_at)`` — the index `idx_send_idem_tenant_customer_created`,
    created for a per-customer frequency lookup that was never written until now.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM send_idempotency_keys "
            " WHERE tenant_id = %s AND customer_id = %s "
            "   AND send_status = ANY(%s) "
            "   AND created_at >= now() - make_interval(hours => %s) "
            " LIMIT 1",
            (str(tenant_id), str(customer_id), list(_DELIVERED), int(hours)),
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unanswerable question is not a "no"
        logger.warning(
            "VT-740 frequency read FAILED tenant=%s customer=%s — suppressing (fail-closed); "
            "a read error must never become a duplicate send",
            tenant_id, customer_id, exc_info=True,
        )
        return None
    return row is not None


def is_suppressed(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any
) -> tuple[bool, str]:
    """``(suppressed, reason)`` for one customer-bound send.

    ``reason`` is a stable machine code for the caller's envelope + the audit line, never prose
    assembled at the call site.
    """
    hours = resolve_interval_hours(tenant_id, customer_id, conn=conn)
    recent = recent_delivery_within(tenant_id, customer_id, hours=hours, conn=conn)
    if recent is None:
        return True, f"frequency_check_unavailable:{hours}h"
    if recent:
        return True, f"recent_delivery_within:{hours}h"
    return False, ""


__all__ = [
    "FAIL_CLOSED_INTERVAL_HOURS",
    "is_suppressed",
    "recent_delivery_within",
    "resolve_interval_hours",
]
