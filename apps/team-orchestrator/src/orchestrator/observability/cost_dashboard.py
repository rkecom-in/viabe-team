"""Cost dashboard read API (VT-103).

Aggregates ``pipeline_log`` ``external_api_call`` events whose payload carries
a ``cost_paise`` integer field into per-tenant breakdowns, workspace
top-N summaries, unit-economics ratios, anomaly flags, and runaway-spend
alert candidates.

Read paths
----------
- ``get_tenant_cost`` opens a ``tenant_connection`` (app_role + GUC) and
  queries ``pipeline_log`` directly — RLS does the isolation.
- All other functions are workspace-level and run under a service-role
  pool connection. They use the ``tenant_cost_daily`` materialised view
  for the fast path (migration 022) and intentionally NEVER grant the
  view to app_role.

Cost-category bucketing follows the OPTIONAL ``cost_category`` convention
documented in :mod:`orchestrator.observability.event_schemas`. When the
payload omits ``cost_category`` we fall back to bucketing by ``vendor``;
when both are absent the bucket is ``other``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from psycopg.rows import dict_row

from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool
from orchestrator.observability.types import (
    CostAnomaly,
    CostRunaway,
    TenantCostBreakdown,
    TenantUnitEconomics,
    WorkspaceCostSummary,
)


# Anomaly + runaway floors (Cowork review §Condition 2).
# Suppress baseline-relative flags when the absolute window cost is too small
# to be operationally meaningful. Tunable via env without a code change.
_ANOMALY_MIN_WINDOW_PAISE: int = int(
    os.getenv("VT_ANOMALY_MIN_WINDOW_PAISE", "10000")
)
_RUNAWAY_MIN_WINDOW_PAISE: int = int(
    os.getenv("VT_RUNAWAY_MIN_WINDOW_PAISE", "10000")
)

# Plan-tier monthly fee in paise. Env-overrideable; defaults match the
# Phase-1 sticker prices stated in the public pricing page.
_PLAN_PRICE_DEFAULTS: dict[str, int] = {
    "founding": 249_900,
    "standard": 499_900,
    "pro": 1_499_900,
}


def _plan_price_paise(plan_tier: str) -> int:
    """Return monthly paise for ``plan_tier``, env-overrideable."""
    env_key = f"{plan_tier.upper()}_PRICE_PAISE"
    raw = os.getenv(env_key)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return _PLAN_PRICE_DEFAULTS.get(plan_tier, 0)


# Known cost categories — payload values outside this set bucket into ``other``.
_KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {"llm", "twilio", "razorpay", "apify", "infra_allocated"}
)


# ---------------------------------------------------------------------------
# 1. Per-tenant breakdown — RLS path
# ---------------------------------------------------------------------------

def get_tenant_cost(
    tenant_id: UUID | str,
    since: datetime,
    until: datetime,
) -> TenantCostBreakdown:
    """Sum ``cost_paise`` from ``pipeline_log.external_api_call`` for one tenant.

    Bucketed by ``payload->>'cost_category'`` (falling back to
    ``payload->>'vendor'`` and then ``'other'``). Uses ``tenant_connection``
    so RLS guarantees the SUM never includes another tenant's rows even if
    the SQL were mis-written.
    """
    by_category: dict[str, int] = {}
    total = 0
    count = 0
    with tenant_connection(tenant_id) as conn, conn.cursor(
        row_factory=dict_row
    ) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(
                    payload->>'cost_category',
                    payload->>'vendor',
                    'other'
                ) AS bucket,
                COALESCE(SUM(NULLIF(payload->>'cost_paise', '')::BIGINT), 0)
                    AS paise,
                COUNT(*) AS n
              FROM pipeline_log
             WHERE tenant_id = %s
               AND event_type = 'external_api_call'
               AND created_at >= %s
               AND created_at < %s
               AND payload ? 'cost_paise'
             GROUP BY 1
            """,
            (str(tenant_id), since, until),
        )
        for row in cur.fetchall():
            bucket_raw = row["bucket"] or "other"
            bucket = bucket_raw if bucket_raw in _KNOWN_CATEGORIES else (
                bucket_raw if bucket_raw == "other" else _bucket_for_unknown(bucket_raw)
            )
            paise = int(row["paise"] or 0)
            n = int(row["n"] or 0)
            by_category[bucket] = by_category.get(bucket, 0) + paise
            total += paise
            count += n
    return TenantCostBreakdown(
        tenant_id=_as_uuid(tenant_id),
        since=since,
        until=until,
        total_paise=total,
        by_category=by_category,
        event_count=count,
    )


def _bucket_for_unknown(raw: str) -> str:
    """Map a free-form vendor string to a known category, falling back to it."""
    lower = raw.lower()
    # Be permissive: many vendor strings encode the category in the name.
    for known in _KNOWN_CATEGORIES:
        if known in lower:
            return known
    return raw


# ---------------------------------------------------------------------------
# 2. Workspace summary — service-role + MV path
# ---------------------------------------------------------------------------

def get_workspace_cost_summary(
    since: datetime,
    until: datetime,
    top_n: int = 10,
) -> WorkspaceCostSummary:
    """Top-N tenants by total cost across the workspace.

    Uses the ``tenant_cost_daily`` materialised view (migration 022). The
    view is service-role-only; callers without privileged role permissions
    receive zero rows.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT tenant_id,
                   COALESCE(SUM(cost_paise), 0) AS paise
              FROM tenant_cost_daily
             WHERE day >= %s::date
               AND day < %s::date
             GROUP BY tenant_id
             ORDER BY paise DESC
             LIMIT %s
            """,
            (since, until, top_n),
        )
        rows = cur.fetchall()
        top: list[tuple[UUID, int]] = [
            (r["tenant_id"], int(r["paise"] or 0)) for r in rows
        ]

        cur.execute(
            """
            SELECT COALESCE(SUM(cost_paise), 0) AS paise
              FROM tenant_cost_daily
             WHERE day >= %s::date
               AND day < %s::date
            """,
            (since, until),
        )
        total_row = cur.fetchone()
        total = int((total_row or {}).get("paise") or 0)

    return WorkspaceCostSummary(
        since=since,
        until=until,
        workspace_total_paise=total,
        top_tenants=top,
    )


# ---------------------------------------------------------------------------
# 3. Unit economics — ARRR / cost ratio
# ---------------------------------------------------------------------------

def get_tenant_unit_economics(
    tenant_id: UUID | str,
    since: datetime,
    until: datetime,
) -> TenantUnitEconomics:
    """Compute the ARRR / cost ratio for ``tenant_id`` over the window.

    ARRR is computed from ``tenants.plan_tier × <PLAN>_PRICE_PAISE`` from env
    config — this approximates monthly subscription revenue, not realised
    revenue. Day-39 calibration (VT-92 evaluator) using this ratio is
    acceptable for plan-fit signals but NOT for actual refund-amount
    calculation. When real revenue events ship (``payment_event`` payloads
    carrying ``amount_paise`` from VT-89 Razorpay wiring), this function
    should switch to sum-of-payment-events scoped to ``(since, until)``.
    Tracked as a known limitation.
    """
    breakdown = get_tenant_cost(tenant_id, since, until)

    # Resolve plan_tier under service role — tenants table is workspace-level.
    plan_tier = _lookup_plan_tier(tenant_id)
    monthly_paise = _plan_price_paise(plan_tier) if plan_tier else 0

    # Pro-rate the monthly fee to the window length.
    window_days = max((until - since).total_seconds() / 86400.0, 0.0)
    arrr_paise = int(round(monthly_paise * (window_days / 30.0)))

    cost = breakdown.total_paise
    if cost == 0:
        ratio = float("inf") if arrr_paise > 0 else 0.0
    else:
        ratio = arrr_paise / cost

    return TenantUnitEconomics(
        tenant_id=_as_uuid(tenant_id),
        arrr_paise=arrr_paise,
        cost_paise=cost,
        ratio=ratio,
    )


def _lookup_plan_tier(tenant_id: UUID | str) -> str | None:
    """Return ``tenants.plan_tier`` (service role; cross-tenant table)."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT plan_tier FROM tenants WHERE id = %s",
            (str(tenant_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row.get("plan_tier")


# ---------------------------------------------------------------------------
# 4. Anomaly detection — baseline-relative with new-tenant + min-window floors
# ---------------------------------------------------------------------------

def detect_cost_anomalies(
    reference_days: int = 28,
    window_days: int = 7,
    multiplier: float = 2.0,
) -> list[CostAnomaly]:
    """Flag tenants whose recent window spend exceeds ``multiplier`` × baseline.

    Default semantics: compare last ``window_days`` mean-per-day to the prior
    ``reference_days - window_days`` window's mean-per-day. Returns one
    :class:`CostAnomaly` per flagged tenant.

    Suppression rules (Cowork review §Condition 2):

    - **New-tenant ineligibility:** if a tenant's baseline average is zero
      (no history in the prior window — joined within the last
      ``reference_days``), they are not flagged. This avoids "all new
      tenants flagged in the first month" false positives.
    - **Minimum absolute window cost:** even if the ratio crosses the
      threshold, suppress when ``window_total < _ANOMALY_MIN_WINDOW_PAISE``
      (default ₹100). Filters spurious flags for tenants with near-zero
      baseline and a tiny absolute spike.
    """
    if window_days < 1 or reference_days < window_days + 1:
        raise ValueError(
            f"reference_days ({reference_days}) must be > window_days "
            f"({window_days})"
        )
    if multiplier <= 0:
        raise ValueError(f"multiplier must be positive, got {multiplier}")

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    baseline_end = window_start
    baseline_start = now - timedelta(days=reference_days)

    flagged: list[CostAnomaly] = []
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH window_costs AS (
                SELECT tenant_id, SUM(cost_paise) AS total_paise
                  FROM tenant_cost_daily
                 WHERE day >= %s::date AND day < %s::date
                 GROUP BY tenant_id
            ),
            baseline_costs AS (
                SELECT tenant_id, SUM(cost_paise) AS total_paise
                  FROM tenant_cost_daily
                 WHERE day >= %s::date AND day < %s::date
                 GROUP BY tenant_id
            )
            SELECT
                w.tenant_id,
                COALESCE(w.total_paise, 0) AS window_paise,
                COALESCE(b.total_paise, 0) AS baseline_paise
              FROM window_costs w
              LEFT JOIN baseline_costs b USING (tenant_id)
            """,
            (window_start, now, baseline_start, baseline_end),
        )
        rows = cur.fetchall()

    baseline_window_days = max(reference_days - window_days, 1)
    for row in rows:
        window_paise = int(row["window_paise"] or 0)
        baseline_paise = int(row["baseline_paise"] or 0)

        # New-tenant ineligibility: no baseline history → not flaggable.
        if baseline_paise == 0:
            continue

        # Minimum absolute window cost floor.
        if window_paise < _ANOMALY_MIN_WINDOW_PAISE:
            continue

        window_avg = window_paise / window_days
        baseline_avg = baseline_paise / baseline_window_days
        ratio = window_avg / baseline_avg if baseline_avg > 0 else 0.0
        if ratio >= multiplier:
            flagged.append(
                CostAnomaly(
                    tenant_id=row["tenant_id"],
                    reference_avg_per_day_paise=int(round(baseline_avg)),
                    window_avg_per_day_paise=int(round(window_avg)),
                    multiplier_observed=ratio,
                )
            )
    return flagged


# ---------------------------------------------------------------------------
# 5. Runaway alert candidates — fraction of monthly plan fee
# ---------------------------------------------------------------------------

def runaway_alert_candidates(
    window_days: int = 7,
    plan_pct_threshold: float = 0.5,
) -> list[CostRunaway]:
    """Tenants whose ``window_days``-window spend exceeds ``plan_pct_threshold``
    × their monthly plan fee.

    Suppression: ignore tenants whose absolute window cost is below
    ``_RUNAWAY_MIN_WINDOW_PAISE`` (default ₹100) — keeps the alert list
    actionable.

    Pure callable; cron wiring is deferred (VT-28). Telegram dispatch is
    deferred (VT-30). The future bot PR consumes this list directly.
    """
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1, got {window_days}")
    if plan_pct_threshold <= 0:
        raise ValueError(
            f"plan_pct_threshold must be positive, got {plan_pct_threshold}"
        )

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    out: list[CostRunaway] = []
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT t.id AS tenant_id,
                   t.plan_tier,
                   COALESCE(SUM(c.cost_paise), 0) AS window_paise
              FROM tenants t
         LEFT JOIN tenant_cost_daily c
                ON c.tenant_id = t.id
               AND c.day >= %s::date
               AND c.day < %s::date
             GROUP BY t.id, t.plan_tier
            """,
            (window_start, now),
        )
        rows = cur.fetchall()

    for row in rows:
        window_paise = int(row["window_paise"] or 0)
        if window_paise < _RUNAWAY_MIN_WINDOW_PAISE:
            continue
        plan_monthly = _plan_price_paise(row["plan_tier"] or "")
        if plan_monthly <= 0:
            continue
        pct = window_paise / plan_monthly
        if pct >= plan_pct_threshold:
            out.append(
                CostRunaway(
                    tenant_id=row["tenant_id"],
                    window_cost_paise=window_paise,
                    plan_monthly_paise=plan_monthly,
                    pct_observed=pct,
                )
            )
    return out


# ---------------------------------------------------------------------------
# 6. Formatter — function-as-tool boundary for the future Telegram bot
# ---------------------------------------------------------------------------

def format_cost_breakdown_for_ops(breakdown: TenantCostBreakdown) -> str:
    """Markdown block ready to drop into a Telegram alert or PR comment.

    No dispatch — the bot wiring lives in VT-30. This formatter is the
    function-as-tool boundary Cowork pre-approved.
    """
    lines = [
        f"**Tenant cost** — `{breakdown.tenant_id}`",
        f"- Window: {breakdown.since.isoformat()} → {breakdown.until.isoformat()}",
        f"- Total: ₹{breakdown.total_paise / 100:.2f} "
        f"({breakdown.event_count} events)",
    ]
    if breakdown.by_category:
        lines.append("- By category:")
        for cat in sorted(breakdown.by_category):
            paise = breakdown.by_category[cat]
            lines.append(f"  - `{cat}`: ₹{paise / 100:.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


# Public surface exported for re-export from ``observability/__init__.py``.
__all__: list[str] = [
    "detect_cost_anomalies",
    "format_cost_breakdown_for_ops",
    "get_tenant_cost",
    "get_tenant_unit_economics",
    "get_workspace_cost_summary",
    "runaway_alert_candidates",
]
