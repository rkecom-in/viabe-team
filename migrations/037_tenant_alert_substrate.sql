-- 037_tenant_alert_substrate.sql — VT-202 proactive alerts substrate.
--
-- Two tables:
--   1. tenant_alert_baselines — rolling p95 latency / cost / volume per
--      tenant. Recomputed every 5 min by the DBOS scheduler. One row
--      per tenant.
--   2. tenant_alerts — every alert that fires. Persists for VT-201
--      history view consumption + retry-on-next-tick fault tolerance.
--      Idempotent retry: dispatcher checks telegram_sent_at /
--      email_sent_at before re-sending.
--
-- Per CL-19 typed columns. Per CL-71 tenant-scoped RLS.
-- Per CL-416 no delete path; DSR-purge owns deletion.

CREATE TABLE IF NOT EXISTS public.tenant_alert_baselines (
    tenant_id            UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    last_computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_p95_ms       INT,
    cost_p95_paise       INT,
    volume_per_hour      INT,
    dispatches_sampled   INT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.tenant_alert_baselines ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_alert_baselines FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_alert_baselines_select ON public.tenant_alert_baselines;
CREATE POLICY tenant_alert_baselines_select ON public.tenant_alert_baselines
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alert_baselines_insert ON public.tenant_alert_baselines;
CREATE POLICY tenant_alert_baselines_insert ON public.tenant_alert_baselines
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alert_baselines_update ON public.tenant_alert_baselines;
CREATE POLICY tenant_alert_baselines_update ON public.tenant_alert_baselines
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alert_baselines_delete ON public.tenant_alert_baselines;
CREATE POLICY tenant_alert_baselines_delete ON public.tenant_alert_baselines
    FOR DELETE USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alert_baselines_operator_select ON public.tenant_alert_baselines;
CREATE POLICY tenant_alert_baselines_operator_select ON public.tenant_alert_baselines
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );


CREATE TABLE IF NOT EXISTS public.tenant_alerts (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    trigger_kind         TEXT NOT NULL CHECK (trigger_kind IN (
        'hard_limit',
        'escalation',
        'error_envelope',
        'cost_anomaly',
        'latency_anomaly',
        'privacy_audit_event',
        'volume_spike',
        'outbound_failure'
    )),
    severity             TEXT NOT NULL CHECK (severity IN ('critical', 'warning')),
    fired_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    dedup_key            TEXT NOT NULL,
    message_text         TEXT NOT NULL,
    telegram_sent_at     TIMESTAMPTZ,
    email_sent_at        TIMESTAMPTZ,
    run_id               UUID,
    payload              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tenant_alerts_tenant_fired
    ON public.tenant_alerts (tenant_id, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_alerts_dedup
    ON public.tenant_alerts (dedup_key, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_tenant_alerts_pending_send
    ON public.tenant_alerts (fired_at)
    WHERE telegram_sent_at IS NULL OR email_sent_at IS NULL;

ALTER TABLE public.tenant_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_alerts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_alerts_select ON public.tenant_alerts;
CREATE POLICY tenant_alerts_select ON public.tenant_alerts
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alerts_insert ON public.tenant_alerts;
CREATE POLICY tenant_alerts_insert ON public.tenant_alerts
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alerts_update ON public.tenant_alerts;
CREATE POLICY tenant_alerts_update ON public.tenant_alerts
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alerts_delete ON public.tenant_alerts;
CREATE POLICY tenant_alerts_delete ON public.tenant_alerts
    FOR DELETE USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_alerts_operator_select ON public.tenant_alerts;
CREATE POLICY tenant_alerts_operator_select ON public.tenant_alerts
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE public.tenant_alert_baselines IS
    'VT-202: per-tenant rolling p95 baselines for anomaly detection. Recomputed every 5 min.';
COMMENT ON TABLE public.tenant_alerts IS
    'VT-202: every alert that fires. Idempotent retry via telegram_sent_at/email_sent_at NULL check on next scheduler tick.';
