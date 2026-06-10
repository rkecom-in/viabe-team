"""VT-86 — monthly impact report generator (deterministic SQL aggregation).

`generate_monthly_report(tenant_id, year_month, conn)` aggregates a tenant's
previous-month activity from the canonical tables into a `MonthlyReport`. NO
LLM on this path (Pillar 1) — every figure is a deterministic SQL aggregate.

The SQL filters `tenant_id` EXPLICITLY (defence-in-depth), and production
callers run it inside `tenant_connection` so RLS is enforced too. Tests inject
a plain tenant-scoped connection.

Scope (Cowork plan-review rulings, 2026-05-30):
  - Campaign-status counts use the 5 REAL states (proposed/approved/rejected/
    sent/failed) — there is no `cancelled` status (descoped).
  - Approval-decision split is the real states only (no needs-changes/defer).
  - Month ARRR = SUM of attributed paise for campaigns whose attribution
    CLOSED in the month.
  - Customer growth = customers created this month vs the prior month.
  - Fees + net value are DESCOPED (no per-month fee ledger; `subscriptions`
    only has a LIFETIME cumulative) — fields stay None for Phase-1, the columns
    are nullable for later backfill (plan D3).

Honesty (Pillar 7): the report carries the raw numbers + boolean flags
(`zero_arrr`, `low_engagement`); the PDF/email layer renders the honest
framing text (i18n). The generator never hides a failure mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.db.wrappers import CampaignsWrapper, CustomersWrapper

# The 5 real campaign lifecycle states (migration 016 CHECK). No 'cancelled'.
CAMPAIGN_STATES: tuple[str, ...] = ("proposed", "approved", "rejected", "sent", "failed")

# Tenant phases that SKIP the report entirely (migration 001 CHECK). `lapsed`
# (VT-365) is a dormant/read-only account with no active subscription → skip.
SKIP_PHASES: frozenset[str] = frozenset({"cancelled", "lapsed"})
# Phases that get the report WITH trial framing rather than a skip.
TRIAL_PHASES: frozenset[str] = frozenset({"onboarding", "trial"})

MIN_DAYS_FOR_REPORT = 30


class TopCampaign(BaseModel):
    """One campaign's ARRR contribution (top-5 ranked)."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    arrr_paise: int


class MonthlyReport(BaseModel):
    """The assembled monthly impact report data (pre-render)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    year_month: str
    business_name: str
    language: str  # 'en' | 'hi' (tenant.preferred_language or language_preference)
    trial_framing: bool  # onboarding/trial → honest "you're in trial" framing

    # Campaign activity (the 5 real states; absent states default to 0).
    campaign_status_counts: dict[str, int]
    approved_count: int
    rejected_count: int
    pending_count: int  # proposed but not yet decided

    # Revenue.
    arrr_paise: int  # SUM attributed paise for campaigns closing this month
    top_campaigns: list[TopCampaign] = Field(default_factory=list)

    # Customer growth.
    customers_added: int
    customers_added_prior_month: int

    # DESCOPED Phase-1 (no per-month fee ledger) — None, columns nullable.
    fees_paid_paise: int | None = None
    net_value_paise: int | None = None

    # Honesty flags (Pillar 7) — the render layer turns these into copy.
    @property
    def zero_arrr(self) -> bool:
        return self.arrr_paise == 0

    @property
    def low_engagement(self) -> bool:
        return self.approved_count < 2

    @property
    def campaigns_sent(self) -> int:
        return self.campaign_status_counts.get("sent", 0)


def month_bounds(year_month: str) -> tuple[datetime, datetime]:
    """Return [start, end) UTC datetimes for a 'YYYY-MM' period (end = first
    instant of the next month)."""
    year, month = (int(p) for p in year_month.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def should_skip(*, phase: str, signed_up_at: datetime | None,
                period_end: datetime) -> str | None:
    """Return a skip reason, or None if the report should be produced.

    - cancelled / lapsed phase → skip.
    - signed up < 30 days before the period end → skip (no full month of data;
      matches VT-3.5's skip rule).
    Trial/onboarding do NOT skip — they get trial framing instead.
    """
    if phase in SKIP_PHASES:
        return f"phase={phase}"
    if signed_up_at is not None:
        # Normalise to tz-aware UTC for the comparison.
        su = signed_up_at if signed_up_at.tzinfo else signed_up_at.replace(tzinfo=timezone.utc)
        if su > period_end - timedelta(days=MIN_DAYS_FOR_REPORT):
            return "signed_up_lt_30d"
    return None


def _scalar(row: Any, key: str, idx: int) -> Any:
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[idx]


def generate_monthly_report(
    tenant_id: str,
    year_month: str,
    *,
    conn: Any,
) -> MonthlyReport | None:
    """Aggregate the month's metrics for one tenant. Returns None if the tenant
    is skipped (caller checks the reason via `should_skip`); otherwise a fully
    populated `MonthlyReport`.

    `conn` is a tenant-scoped psycopg connection (production: inside
    `tenant_connection`; tests: a direct connection). All SQL filters
    `tenant_id` explicitly so results are correct regardless of RLS role.
    """
    start, end = month_bounds(year_month)
    prior_start, prior_end = month_bounds(_prior_year_month(year_month))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT business_name, phase, signed_up_at, "
            "COALESCE(preferred_language, language_preference, 'en') AS lang "
            "FROM tenants WHERE id = %s",
            (tenant_id,),
        )
        trow = cur.fetchone()
        if trow is None:
            return None
        business_name = _scalar(trow, "business_name", 0)
        phase = _scalar(trow, "phase", 1)
        signed_up_at = _scalar(trow, "signed_up_at", 2)
        language = _scalar(trow, "lang", 3) or "en"

        if should_skip(phase=phase, signed_up_at=signed_up_at, period_end=end):
            return None

        # VT-306: campaign reads via CampaignsWrapper on the caller's tenant-scoped
        # cur. The attributions side of the ARRR joins is scoped within the wrapper.
        cw = CampaignsWrapper()

        # 1. Campaign-status counts (5 real states), filtered by generated_at.
        counts = {s: 0 for s in CAMPAIGN_STATES}
        for st, n in cw.count_by_status_in_range(tenant_id, start, end, conn=cur).items():
            if st in counts:
                counts[st] = n

        # 2. Month ARRR — attributed paise for campaigns CLOSING this month.
        arrr_paise = cw.sum_arrr_closed_in_range(tenant_id, start, end, conn=cur)

        # 3. Top-5 campaigns by ARRR contribution this month.
        top = [
            TopCampaign(campaign_id=r["cid"], arrr_paise=int(r["arrr"] or 0))
            for r in cw.top_campaigns_by_arrr_in_range(tenant_id, start, end, conn=cur)
        ]

        # 4. Customer growth — this month vs prior month (created_at).
        customers_added = _count_customers(cur, tenant_id, start, end)
        customers_prior = _count_customers(cur, tenant_id, prior_start, prior_end)

    return MonthlyReport(
        tenant_id=tenant_id,
        year_month=year_month,
        business_name=business_name,
        language=language,
        trial_framing=phase in TRIAL_PHASES,
        campaign_status_counts=counts,
        approved_count=counts["approved"],
        rejected_count=counts["rejected"],
        pending_count=counts["proposed"],
        arrr_paise=arrr_paise,
        top_campaigns=top,
        customers_added=customers_added,
        customers_added_prior_month=customers_prior,
        # fees/net descoped — left None (Phase-1).
    )


def _count_customers(cur: Any, tenant_id: str, start: datetime, end: datetime) -> int:
    # VT-306: via the wrapper on the caller's tenant-scoped cur.
    return CustomersWrapper().count_created_in_range(tenant_id, start, end, conn=cur)


def _prior_year_month(year_month: str) -> str:
    year, month = (int(p) for p in year_month.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


__all__ = [
    "CAMPAIGN_STATES",
    "MonthlyReport",
    "TopCampaign",
    "generate_monthly_report",
    "month_bounds",
    "should_skip",
]
