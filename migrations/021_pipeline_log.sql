-- 021_pipeline_log.sql — append-only structured event store (VT-102).
--
-- Every event in the orchestrator / agent / tool / webhook / scheduled
-- trigger / refund / error path writes a row. PII redacted at write time by
-- the writer (orchestrator/observability/log.py via observability/pii.py).
-- Tenant-scoped where applicable; workspace-level events have tenant_id NULL
-- and are visible only via the service role (no SELECT policy targets NULL
-- under app_role — RLS denies by default).
--
-- Append-only structural via RLS (Pillar 7): only INSERT + SELECT policies
-- exist for app_role. UPDATE / DELETE have NO policy, so app_role gets
-- `permission denied` at the policy layer. Service role retains full bypass
-- for the retention sweep.
--
-- Retention: 90 days, swept by the service-role-only function
-- `purge_pipeline_log_older_than(days INT)` invoked nightly (cron wiring is
-- Phase 2, out of scope here per brief).

CREATE TABLE pipeline_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL,
    tenant_id     UUID REFERENCES tenants (id),
    event_type    TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warn', 'error', 'critical')),
    component     TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms   INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes match the brief's expected query patterns.
CREATE INDEX pipeline_log_run_id_created_idx
    ON pipeline_log (run_id, created_at DESC);

CREATE INDEX pipeline_log_tenant_created_idx
    ON pipeline_log (tenant_id, created_at DESC)
    WHERE tenant_id IS NOT NULL;

CREATE INDEX pipeline_log_event_type_created_idx
    ON pipeline_log (event_type, created_at DESC);

CREATE INDEX pipeline_log_severity_created_idx
    ON pipeline_log (severity, created_at DESC)
    WHERE severity IN ('error', 'critical');

-- Pillar 3 + Pillar 7: RLS in the same migration that creates the table.
ALTER TABLE pipeline_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_log FORCE ROW LEVEL SECURITY;

-- Tenant rows visible to that tenant's app_role connection.
-- Workspace-level (tenant_id IS NULL) rows are NOT covered by this policy,
-- so app_role can't read them; service role bypasses RLS entirely.
CREATE POLICY pipeline_log_select ON pipeline_log FOR SELECT
    USING (tenant_id = app_current_tenant());

-- INSERT under app_role is allowed only when tenant_id matches the GUC.
-- Workspace-level (tenant_id IS NULL) inserts must come through the service
-- role connection, which bypasses RLS.
CREATE POLICY pipeline_log_insert ON pipeline_log FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());

-- DELIBERATELY NO UPDATE / DELETE POLICIES.
-- app_role tries to UPDATE or DELETE → `permission denied for table
-- pipeline_log`. Service role bypasses RLS for the retention sweep.

GRANT SELECT, INSERT ON pipeline_log TO app_role;
-- Migration `015_app_role.sql` set ALTER DEFAULT PRIVILEGES granting
-- SELECT/INSERT/UPDATE/DELETE on future tables to app_role; explicitly
-- REVOKE the mutating permissions so the append-only contract is structural.
REVOKE UPDATE, DELETE, TRUNCATE ON pipeline_log FROM app_role;

-- ---------------------------------------------------------------------------
-- Retention sweep (90-day default; callable by service role only).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION purge_pipeline_log_older_than(retention_days INT DEFAULT 90)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM pipeline_log
    WHERE created_at < now() - (retention_days || ' days')::INTERVAL;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

-- Function is SECURITY DEFINER, owned by the privileged migration-runner role,
-- so calling it always runs with bypass-RLS privileges. Revoke from PUBLIC +
-- app_role so only service-role callers (postgres / supabase-secret) can
-- invoke it.
REVOKE ALL ON FUNCTION purge_pipeline_log_older_than(INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION purge_pipeline_log_older_than(INT) FROM app_role;

COMMENT ON TABLE pipeline_log IS
    'Append-only structured event store (VT-102). Writer: '
    'orchestrator/observability/log.py. Retention: '
    'purge_pipeline_log_older_than(days) — service role only. '
    'tenant_id NULL = workspace-level event, service-role-readable.';
