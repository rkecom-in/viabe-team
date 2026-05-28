-- 034_tenant_connector_status.sql — VT-210 recurring ingestion substrate.
--
-- Operational state per (tenant, connector). Distinct from tenant_integration_state
-- (031), which tracks one-tenant onboarding-phase flow. This table is the per-pair
-- runtime status: when did the last sync run, did it succeed, how many consecutive
-- failures, what is the cron cadence, when is the next scheduled run, etc.
--
-- Single fan-out scheduler (VT-210 Q1): @DBOS.scheduled('*/5 * * * *') scans
-- enabled rows where next_scheduled_run <= now() and dispatches per-pair
-- ingest_one_connector workflows.
--
-- Per CL-19 typed columns (no JSONB blob).
-- Per CL-71 tenant-scoped RLS.
-- Per CL-416 no delete path; DSR-purge owns deletion.

CREATE TABLE IF NOT EXISTS public.tenant_connector_status (
    tenant_id             UUID NOT NULL REFERENCES tenants(id),
    connector_id          TEXT NOT NULL,
    pull_cadence          TEXT NOT NULL DEFAULT '0 9 * * *',
    last_sync_at          TIMESTAMPTZ,
    last_status           TEXT CHECK (last_status IN ('ok','error','pending','disabled')),
    last_error_message    TEXT,
    consecutive_fails     INT NOT NULL DEFAULT 0 CHECK (consecutive_fails >= 0),
    rows_ingested_today   INT NOT NULL DEFAULT 0 CHECK (rows_ingested_today >= 0),
    last_ingested_date    DATE,
    next_scheduled_run    TIMESTAMPTZ,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connector_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_connector_status_next_run
    ON public.tenant_connector_status (next_scheduled_run)
    WHERE enabled = TRUE;

ALTER TABLE public.tenant_connector_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_connector_status FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_connector_status_select ON public.tenant_connector_status;
CREATE POLICY tenant_connector_status_select ON public.tenant_connector_status
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_connector_status_insert ON public.tenant_connector_status;
CREATE POLICY tenant_connector_status_insert ON public.tenant_connector_status
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_connector_status_update ON public.tenant_connector_status;
CREATE POLICY tenant_connector_status_update ON public.tenant_connector_status
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_connector_status_delete ON public.tenant_connector_status;
CREATE POLICY tenant_connector_status_delete ON public.tenant_connector_status
    FOR DELETE USING (tenant_id = app_current_tenant());

-- Operator-claim SELECT (mirrors migration 030/031/032 pattern).
DROP POLICY IF EXISTS tenant_connector_status_operator_select ON public.tenant_connector_status;
CREATE POLICY tenant_connector_status_operator_select ON public.tenant_connector_status
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE public.tenant_connector_status IS
    'VT-210 recurring-ingestion operational state. PK (tenant_id, connector_id). Scheduler scans WHERE enabled AND next_scheduled_run <= now().';
