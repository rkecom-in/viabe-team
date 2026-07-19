"""VT-563 — attribution-outcome PRODUCER (un-severs the outcome-learning leg).

The ``attributions`` table (mig 023 + 047) had NO production writer — VT-175's
aggregator (``attribution_close``) only READ it, and it was always empty, so the
whole outcome-learning leg was severed: ``get_attribution_data`` / context
``recovered_paise`` were always 0 and the implicit-feedback sweep (VT-198/432)
was a permanent no-op.

This module is that writer. At attribution close (``attribution_close.close_
attribution``, the race-winner path) it produces the ``attributions`` rows for
the campaign being closed by joining the campaign's recipients
(``campaign_recipients``, populated at collapse — VT-241) to their PAYMENT
ledger entries (``customer_ledger_entries`` ``entry_type='payment'``, populated
by ingestion — VT-273) inside the attribution window, one row per qualifying
payment.

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
) -> int:
    """Produce ``attributions`` rows for one campaign; return the count inserted.

    ``cur`` is a service-role cursor inside the caller's transaction
    (``close_attribution``). Joins the campaign's recipients to their PAYMENT
    ledger entries in the window ``[close_at - ATTRIBUTION_WINDOW_DAYS,
    close_at]`` (inclusive, on ``entry_date``) and inserts one ``attributions``
    row per payment. Idempotent by construction: the caller runs this exactly
    once per campaign (the close-race winner), so no payment is attributed twice.

    ``attribution_method`` is ``window_match`` — the credit is a
    recipient-paid-within-the-window inference (no VPA/amount corroboration to a
    specific outreach), never over-claiming ``exact_match`` (mirrors
    match_transactions' conservative fallback). ``attribution_confidence`` is the
    ledger entry's capture confidence (``source_confidence``, already in [0, 1]).
    ``razorpay_payment_id`` stays NULL — these are ledger-sourced, not
    Razorpay-sourced, payments.
    """
    tid = str(tenant_id)
    cid = str(campaign_id)
    window_start = (close_at - timedelta(days=ATTRIBUTION_WINDOW_DAYS)).date()
    window_end = close_at.date()
    cur.execute(
        """
        INSERT INTO attributions
            (tenant_id, campaign_id, customer_id, attributed_paise,
             attribution_method, attribution_confidence, attribution_at)
        SELECT cr.tenant_id, cr.campaign_id, cle.customer_id, cle.amount_paise,
               'window_match', cle.source_confidence, now()
        FROM campaign_recipients cr
        JOIN customer_ledger_entries cle
          ON cle.tenant_id = cr.tenant_id
         AND cle.customer_id = cr.customer_id
        WHERE cr.tenant_id = %s
          AND cr.campaign_id = %s
          AND cle.entry_type = 'payment'
          AND cle.entry_date >= %s
          AND cle.entry_date <= %s
        """,
        (tid, cid, window_start, window_end),
    )
    n = cur.rowcount or 0
    logger.info(
        "build_campaign_attributions: tenant=%s campaign=%s produced=%d",
        tid, cid, n,
    )
    return n


__all__ = ["ATTRIBUTION_WINDOW_DAYS", "build_campaign_attributions"]
