"""VT-202 — per-tenant rolling p95 baseline computation.

Pure SQL window-function CTE over ``pipeline_runs`` last-100-per-
tenant. Persists into ``tenant_alert_baselines``. Idempotent +
restart-safe (the table is an upsert target; concurrent recomputes
collide harmlessly).

Cowork-locked architecture (2026-05-28): NOT in-process aggregation
— Postgres window functions keep the work close to the data.
"""

from __future__ import annotations

import logging

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


_BASELINE_SQL = """
WITH ranked AS (
    SELECT
        tenant_id,
        total_cost_paise,
        EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 AS latency_ms,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id ORDER BY started_at DESC
        ) AS rn
    FROM pipeline_runs
    WHERE ended_at IS NOT NULL
      AND started_at > now() - interval '30 days'
),
last_100 AS (
    SELECT *
    FROM ranked
    WHERE rn <= 100
),
agg AS (
    SELECT
        tenant_id,
        COUNT(*)::int AS dispatches_sampled,
        PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY latency_ms)::int
            AS latency_p95_ms,
        PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY total_cost_paise)::int
            AS cost_p95_paise
    FROM last_100
    GROUP BY tenant_id
),
volume AS (
    SELECT
        tenant_id,
        (COUNT(*) / 24)::int AS volume_per_hour
    FROM pipeline_runs
    WHERE started_at > now() - interval '24 hours'
    GROUP BY tenant_id
)
INSERT INTO tenant_alert_baselines (
    tenant_id, last_computed_at, latency_p95_ms, cost_p95_paise,
    volume_per_hour, dispatches_sampled
)
SELECT
    a.tenant_id,
    now(),
    a.latency_p95_ms,
    a.cost_p95_paise,
    COALESCE(v.volume_per_hour, 0),
    a.dispatches_sampled
FROM agg a
LEFT JOIN volume v USING (tenant_id)
ON CONFLICT (tenant_id) DO UPDATE SET
    last_computed_at = EXCLUDED.last_computed_at,
    latency_p95_ms = EXCLUDED.latency_p95_ms,
    cost_p95_paise = EXCLUDED.cost_p95_paise,
    volume_per_hour = EXCLUDED.volume_per_hour,
    dispatches_sampled = EXCLUDED.dispatches_sampled;
"""


def recompute_tenant_baselines() -> int:
    """Recompute baselines for every tenant with ≥1 terminal run.

    Returns the count of tenant rows touched.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_BASELINE_SQL)
        rowcount = cur.rowcount if cur.rowcount is not None else 0
    logger.info("tenant_alert_baselines: upserted %d row(s)", rowcount)
    return rowcount


__all__ = ["recompute_tenant_baselines"]
