"""VT-563 — attribution-outcome PRODUCER (un-severs the outcome-learning leg).

The ``attributions`` table (mig 023 + 047) had NO production writer — VT-175's
aggregator (``attribution_close``) only READ it, and it was always empty, so the
whole outcome-learning leg was severed: ``get_attribution_data`` / context
``recovered_paise`` were always 0 and the implicit-feedback sweep (VT-198/432)
was a permanent no-op.

This module is that writer. At attribution close (``attribution_close.close_
attribution``, the race-winner path) it produces the ``attributions`` rows for
the campaign being closed.

**RULING D-C (Fazal 2026-08-15, CL-2026-08-15-three-m2b-rulings): ATTRIBUTION
ERRS UNDER, NEVER OVER. Only two things count — a tracked link/code, or a reply
followed by a purchase inside the window.**

That replaced what this writer did. It used to join the campaign's recipients to
any ledger entry in the window and credit the campaign with it. Two things were
wrong with that, and only the first was noticed:

  1. The predicate read ``entry_type = 'payment'`` while every production
     producer writes ``'sale'`` (``ingest``, ``upi_export``, ``_image_adapter``,
     ``imported_transactions``), so it matched nothing and the table has been
     empty since VT-417. That is the bug that made the defect visible.
  2. **Correcting the vocabulary alone would have produced a WRONG non-zero.** A
     shop's sales continue whether or not we messaged anyone, so crediting a
     coincident sale claims revenue the campaign did not cause — at scale, in an
     owner-facing number. VT-754 recommended exactly that and the ruling rejected
     it; the recommendation was solving "make the join match the data" when the
     question was "what did we actually cause".

So a qualifying row now needs an ATTRIBUTABLE SIGNAL that precedes the purchase:

  * ``tracked_link`` — the recipient clicked a tracked link (VT-745's
    ``customer_hook_links``), then purchased.
  * ``reply_then_purchase`` — the recipient replied to us
    (``wa_conversations.last_inbound_at``), then purchased.

Both are joined so the SIGNAL comes FIRST and the purchase follows, inside the
window. A purchase that precedes its "signal" is a coincidence with better
timing, not a cause.

**This will produce ZERO until a signal feed exists, and that is the correct
behaviour, not a regression.** VT-745 records that nothing mints a customer hook
link yet, so the tracked-link half finds nothing today; the reply half is live
but needs the recipient's phone token, which only the caller can compute.
Erring under is the ruling.

**ZERO LLM invocations** (Pillar 1) — pure SQL. It runs on the service-role
connection INSIDE ``close_attribution``'s race-won transaction, so it executes
EXACTLY ONCE per campaign (the close UPDATE is the idempotency guard) and is
atomic with the close.

Tables touched — ``campaign_recipients`` / ``customer_ledger_entries`` /
``attributions`` — are NOT in the ``no-direct-tenant-db-access`` watched set
(customers/campaigns/...), so this module reads them directly. Every statement
carries an explicit ``tenant_id = %s`` predicate because the caller's
connection is the BYPASSRLS service pool (RLS is inert there) — the same
discipline ``attribution_close`` uses for its by-PK ``campaigns`` UPDATE.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# The standard attribution window: a campaign earns credit for a recipient's
# payment made within this many days up to attribution_close_at (which is
# ``send_at + ATTRIBUTION_WINDOW_DAYS`` per the campaign_plan close-at validator).
ATTRIBUTION_WINDOW_DAYS = 7


def build_campaign_attributions(
    cur: Any,
    tenant_id: UUID | str,
    campaign_id: UUID | str,
    close_at: datetime,
    *,
    reply_tokens: list[str] | None = None,
) -> int:
    """Produce ``attributions`` rows for one campaign; return the count inserted.

    ``cur`` is a service-role cursor inside the caller's transaction
    (``close_attribution``). Joins the campaign's recipients to their PAYMENT
    ledger entries in the window ``[close_at - ATTRIBUTION_WINDOW_DAYS,
    close_at]`` (inclusive, on ``entry_date``) **that follow an attributable
    signal** — see the module docstring for ruling D-C. One row per qualifying
    purchase. Idempotent by construction: the caller runs this exactly once per
    campaign (the close-race winner), so no purchase is attributed twice.

    ``reply_tokens`` — the ``wa_conversations.phone_token`` values for this
    campaign's recipients, which the CALLER must supply. They cannot be derived
    here: the token is a SALTED SHA-256 whose salt is an application secret, so
    computing it in SQL would put that secret in a query string and any other
    derivation simply would not match what the inbound path wrote (the same
    constraint ``send_frequency._phone_token_for_phone`` documents). Passing
    ``None``/empty disables the reply half — the tracked-link half still runs,
    and the result errs UNDER, which is the ruling's direction.

    ``attribution_method`` names WHICH signal earned the credit
    (``tracked_link`` / ``reply_then_purchase``, migration 206) rather than the
    legacy ``window_match``, so a reader can tell what was actually checked
    without joining anything. ``attribution_confidence`` is the ledger entry's
    capture confidence (``source_confidence``, already in [0, 1]).
    ``razorpay_payment_id`` stays NULL — these are ledger-sourced.
    """
    tid = str(tenant_id)
    cid = str(campaign_id)
    window_start = (close_at - timedelta(days=ATTRIBUTION_WINDOW_DAYS)).date()
    window_end = close_at.date()
    tokens = list(reply_tokens or [])
    cur.execute(
        """
        INSERT INTO attributions
            (tenant_id, campaign_id, customer_id, attributed_paise,
             attribution_method, attribution_confidence, attribution_at)
        SELECT cr.tenant_id, cr.campaign_id, cle.customer_id, cle.amount_paise,
               sig.method, cle.source_confidence, now()
        FROM campaign_recipients cr
        JOIN customer_ledger_entries cle
          ON cle.tenant_id = cr.tenant_id
         AND cle.customer_id = cr.customer_id
        JOIN LATERAL (
            -- The attributable signal, STRONGEST FIRST. A tracked link is an act aimed at us; a
            -- reply is an act aimed at us that we cannot bind to a specific outbound (VT-744
            -- records that gap as PARTIAL — last_inbound_at is a mutable timestamp, not an event
            -- attributed to a message). Ordering by method name happens to put 'reply_then_purchase'
            -- before 'tracked_link' alphabetically, so the ORDER BY is explicit rather than
            -- incidental.
            SELECT 'tracked_link' AS method, k.last_clicked_at AS signal_at, 1 AS rank
              FROM customer_hook_links k
             WHERE k.tenant_id = cr.tenant_id
               AND k.customer_id = cr.customer_id
               AND k.last_clicked_at IS NOT NULL
               AND k.last_clicked_at::date >= %s
               AND k.last_clicked_at::date <= %s
            UNION ALL
            SELECT 'reply_then_purchase', w.last_inbound_at, 2
              FROM wa_conversations w
             WHERE w.tenant_id = cr.tenant_id
               AND w.phone_token = ANY(%s)
               AND w.last_inbound_at IS NOT NULL
               AND w.last_inbound_at::date >= %s
               AND w.last_inbound_at::date <= %s
            ORDER BY rank
            LIMIT 1
        ) sig ON TRUE
        WHERE cr.tenant_id = %s
          AND cr.campaign_id = %s
          -- VT-754: the vocabulary every production producer actually writes. 'payment' is kept
          -- because historical rows carry it; the CI gate in test_vt754_* enumerates readers against
          -- producers so this list cannot silently drift again.
          AND cle.entry_type IN ('sale', 'payment')
          AND cle.entry_date >= %s
          AND cle.entry_date <= %s
          -- The purchase must FOLLOW the signal. Without this the join credits a sale that happened
          -- before the customer ever clicked or replied.
          AND cle.entry_date >= sig.signal_at::date
        """,
        (
            window_start, window_end,
            tokens, window_start, window_end,
            tid, cid, window_start, window_end,
        ),
    )
    n = cur.rowcount or 0
    logger.info(
        "build_campaign_attributions: tenant=%s campaign=%s produced=%d",
        tid, cid, n,
    )
    return n


__all__ = ["ATTRIBUTION_WINDOW_DAYS", "build_campaign_attributions"]
