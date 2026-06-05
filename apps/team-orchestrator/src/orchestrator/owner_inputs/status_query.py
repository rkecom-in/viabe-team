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

StatusQueryType = Literal["customer_count", "last_campaign", "opt_out_count", "billing", "unknown"]

_DASHBOARD = "https://viabe.ai/team/dashboard"


def classify_status_query(body: str) -> StatusQueryType:
    """Keyword-route the query type. Opt-out is checked first (so 'how many opted-out
    customers' is an opt_out_count, not a customer_count)."""
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}
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
    if {"campaign", "campaigns"} & tokens:
        return "last_campaign"
    if {"customer", "customers", "ग्राहक", "ग्राहकों"} & tokens:
        return "customer_count"
    if {"trial", "billing", "plan", "subscription", "phase"} & tokens:
        return "billing"
    return "unknown"


def answer_status_query(tenant_id: UUID | str, body: str) -> str:
    """Return the templated answer text for the owner's status query (deterministic SQL)."""
    from orchestrator.db.wrappers import CustomersWrapper

    qtype = classify_status_query(body)

    if qtype == "customer_count":
        n = CustomersWrapper().count_all(tenant_id)
        return f"You currently have {n} customers in your ledger."

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

    return f"For detailed answers, check your Viabe Team portal at {_DASHBOARD}"
