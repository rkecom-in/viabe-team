-- 022_tenant_cost_views.sql — per-tenant cost aggregation (VT-103).
--
-- Materialised view `tenant_cost_daily` rolls up `pipeline_log.external_api_call`
-- events by tenant_id × day × event_type × cost_category. Service-role-only
-- (no GRANT to app_role — workspace queries are intentionally cross-tenant
-- and run under the privileged role).
--
-- `get_tenant_cost` (Python) queries `pipeline_log` DIRECTLY under
-- `tenant_connection()`, so RLS enforces tenant isolation on that read path.
-- The materialised view is only used for the workspace top-N / anomaly /
-- runaway paths.
--
-- CONCURRENTLY refresh requires a UNIQUE index — added below.

CREATE MATERIALIZED VIEW tenant_cost_daily AS
SELECT
    tenant_id,
    DATE(created_at) AS day,
    event_type,
    COALESCE(payload->>'cost_category', payload->>'vendor', 'unknown') AS category,
    SUM(NULLIF(payload->>'cost_paise', '')::BIGINT) AS cost_paise,
    COUNT(*) AS event_count
FROM pipeline_log
WHERE event_type = 'external_api_call'
  AND tenant_id IS NOT NULL
  AND payload ? 'cost_paise'
GROUP BY 1, 2, 3, 4;

-- UNIQUE index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX tenant_cost_daily_pk
    ON tenant_cost_daily (tenant_id, day, event_type, category);

-- Lookup indexes for the common query patterns.
CREATE INDEX tenant_cost_daily_tenant_day_idx
    ON tenant_cost_daily (tenant_id, day DESC);

CREATE INDEX tenant_cost_daily_day_cost_idx
    ON tenant_cost_daily (day DESC, cost_paise DESC);

-- Service-role only. NOT granted to app_role — workspace queries (top-N,
-- anomaly, runaway) run as the privileged migration-owner role; tenant
-- queries use the RLS-enforced raw pipeline_log path.

COMMENT ON MATERIALIZED VIEW tenant_cost_daily IS
    'Per-tenant daily cost aggregation (VT-103). Refresh: '
    'REFRESH MATERIALIZED VIEW CONCURRENTLY tenant_cost_daily. '
    'Source: pipeline_log.external_api_call events whose payload carries '
    'a numeric cost_paise field. Service-role only.';
