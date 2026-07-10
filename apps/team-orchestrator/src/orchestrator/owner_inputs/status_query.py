"""VT-84 — owner status-query handler (DETERMINISTIC; NEVER the agent).

The owner asks a fact about THEIR OWN data; we answer with a templated SQL aggregation.
Query-type parse is keyword-based, VT-329-safe (NFC + whitespace/punct split, no
Devanagari-dead `\\b`). Unknown queries fall back to the portal link.

# NEEDS-FAZAL: the response copy (Pillar 7 — owner-facing words) is placeholder; Fazal
reviews wording later. The LOGIC (which SQL, which number) is what lands now.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from uuid import UUID

from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS

StatusQueryType = Literal[
    "customer_count", "lapsed_count", "last_campaign", "opt_out_count", "billing", "unknown"
]

_DASHBOARD = "https://viabe.ai/team/dashboard"

# VT-632 — Fazal's canonical customer-facing definition (2026-07-09; unified CL-2026-07-10): a
# LAPSED / dormant customer is one with NO purchase in the last ``LAPSED_WINDOW_DAYS`` days. The
# constant is defined in ``db.wrappers`` (imported above) as the SINGLE SOURCE OF TRUTH — the only
# runtime lapsed-window value; reference it, never re-literal 45. Since CL-2026-07-10 (option 2) the
# Sales-Recovery SEND cohort uses this SAME window (no longer the VT-312 percentile), so the number
# the owner hears here IS the exact set a win-back campaign targets.

# VT-632 — a cash-flow / receivables / finance READ is NOT a status_query this deterministic parse
# owns (there is no such qtype); it belongs to the brain's finance advisory tools (analyze_cash_flow).
# Guarded FIRST (below) so a NEGATED or stray 'campaigns'/'customers' token in the SAME message
# ("...only the number, no drafts, no messages, no campaigns") cannot hijack a finance ask into a
# canned last_campaign/customer_count answer — the efficient_no_overstep wrong-read where an owner's
# cash-flow question got answered "You haven't run a campaign in the last 30 days."
_FINANCE_READ_TOKENS = frozenset({
    "cash", "cashflow", "receivable", "receivables", "revenue", "profit", "margin",
    "turnover", "collections", "collection", "outstanding", "dues", "income",
})


def classify_status_query(body: str) -> StatusQueryType:
    """Keyword-route the query type. Opt-out is checked first (so 'how many opted-out
    customers' is an opt_out_count, not a customer_count). VT-632: a finance/cash-flow read is
    guarded out FIRST (returns 'unknown' -> falls through to the brain) so a negated 'campaigns'
    token in the same message can't hijack it."""
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}
    if (_FINANCE_READ_TOKENS & tokens) or "cash flow" in norm:
        return "unknown"
    if (
        "opted" in tokens
        or "optout" in tokens
        or "optouts" in tokens
        or "unsubscribed" in tokens
        or "excluded" in tokens
        or "opt-out" in norm
        or "opt out" in norm
    ):
        return "opt_out_count"
    # VT-632 lapsed_count — checked BEFORE customer_count so "how many LAPSED customers" answers the
    # dormant count (45d), not the total ledger count (the sr_cohort defect: "10 total" for a lapsed
    # ask whose true answer is the dormant subset). Keyed on the explicit "lapsed"/"dormant" TOKEN
    # (Cowork 202500Z) — NOT behavioural phrases like "haven't bought" (those stay with the brain's
    # speech-act guard, and a DO like "win back my lapsed customers" never classifies status_query).
    if {"lapsed", "dormant"} & tokens:
        return "lapsed_count"
    # A SEND-STATUS question ("did you send it?", "already sent?", "has the message gone out?") is a
    # read about whether a campaign/send actually happened — route it to last_campaign so the owner
    # gets an honest "you haven't run a campaign" (= no, nothing sent) / "your last campaign…" answer.
    # Checked BEFORE customer_count so a stray "customers" token ("did you send it to my old
    # CUSTOMERS?") cannot hijack a send-status ask into a ledger COUNT — the m_honesty_fabricated_
    # campaign non-sequitur ("You currently have N customers in your ledger", official §2 2026-07-10).
    # Send IMPERATIVES never reach here: the upstream classifier routes a real send to
    # adhoc_campaign_request / new_task, never status_query, so a send token here always means "asking
    # ABOUT a send", never "do a send".
    if ({"sent", "send", "sending"} & tokens) or ("go out" in norm) or ("gone out" in norm) or (
        "went out" in norm
    ):
        return "last_campaign"
    if {"campaign", "campaigns"} & tokens:
        return "last_campaign"
    if {"customer", "customers", "ग्राहक", "ग्राहकों"} & tokens:
        return "customer_count"
    if {"trial", "billing", "plan", "subscription", "phase"} & tokens:
        return "billing"
    return "unknown"


def answer_status_query(tenant_id: UUID | str, body: str) -> str | None:
    """Return the templated answer text for the owner's status query (deterministic SQL).

    VT-600 — returns ``None`` when the keyword parse can't name a query type it
    genuinely answers ('unknown'). The old behavior deflected to the portal
    ("For detailed answers, check your Viabe Team portal…"), which the VT-598
    opus judge flagged live: the classifier tags conversational confirmations
    ("did you get my store address?") as status_query, the parse finds no
    count/campaign/billing token, and the owner got a canned deflection instead
    of an answer. Per the VT-588 seam pattern: a fast-path handles ONLY what it
    understands; everything else falls through to the manager brain (the router
    returns None on None)."""
    from orchestrator.db.wrappers import CustomersWrapper

    qtype = classify_status_query(body)

    if qtype == "customer_count":
        n = CustomersWrapper().count_all(tenant_id)
        return f"You currently have {n} customers in your ledger."

    if qtype == "lapsed_count":
        # Fazal's 45d definition: bought before, no sale in the last LAPSED_WINDOW_DAYS.
        cw = CustomersWrapper()
        # EMPTY-LEDGER honesty (sr_empty_cohort_honesty): a lapsed count of 0 is AMBIGUOUS — it means
        # either "all bought recently" OR "no sales data at all". Only claim the former when a sales
        # base actually exists; otherwise say we have no data (never fabricate "everyone bought
        # within 45 days" against an empty ledger).
        if cw.count_with_sales(tenant_id) == 0:
            return (
                "I don't have any sales history for your customers yet — connect a data source and "
                "I'll show you exactly who's gone quiet."
            )
        n = cw.count_lapsed(tenant_id, days=LAPSED_WINDOW_DAYS)
        if n == 0:
            return (
                "None of your customers are lapsed — everyone with a purchase history has bought "
                f"within the last {LAPSED_WINDOW_DAYS} days."
            )
        return (
            f"{n} of your customers are lapsed — they bought before but haven't in the last "
            f"{LAPSED_WINDOW_DAYS} days."
        )

    if qtype == "opt_out_count":
        # opted_out (consumer) + owner_excluded (owner) are both skipped by campaign sends.
        n = CustomersWrapper().count_by_opt_out_status(tenant_id, ("opted_out", "owner_excluded"))
        return f"{n} customers are excluded from your campaigns (opted out or owner-excluded)."

    if qtype == "last_campaign":
        from orchestrator.agent.tools.get_recent_campaigns import (
            GetRecentCampaignsInput,
            get_recent_campaigns,
        )

        out = get_recent_campaigns(
            GetRecentCampaignsInput(tenant_id=str(tenant_id), days_back=30, limit=1)
        )
        if not out.campaigns:
            return "You haven't run a campaign in the last 30 days."
        c = out.campaigns[0]
        # NOTE: recipients_count is 1 per campaign row in the current model, so we report
        # responses + status (not a recipient count, which would be misleading as "1").
        return f"Your last campaign got {c.response_count} responses (status: {c.status})."

    if qtype == "billing":
        # Phase/trial detail lives in the portal; keep this a pointer (Pillar-7 copy TBD).
        return f"Your trial/billing status is on your portal: {_DASHBOARD}"

    # 'unknown' — not a lookup this fast-path owns; the brain answers (VT-600).
    return None
