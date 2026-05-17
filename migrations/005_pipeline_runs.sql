-- 005_pipeline_runs.sql — one row per orchestrator/specialist pipeline run.
CREATE TABLE pipeline_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants (id),
    run_type                TEXT,
    status                  TEXT NOT NULL CHECK (status IN (
                                'running', 'completed', 'escalated',
                                'aborted_hard_limit', 'duplicate_rejected')),
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at                TIMESTAMPTZ,
    trigger_payload         JSONB,
    terminal_state_metadata JSONB,
    cost_paise              BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX pipeline_runs_tenant_idx ON pipeline_runs (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration.
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs FORCE ROW LEVEL SECURITY;

CREATE POLICY pipeline_runs_select ON pipeline_runs FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY pipeline_runs_insert ON pipeline_runs FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pipeline_runs_update ON pipeline_runs FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pipeline_runs_delete ON pipeline_runs FOR DELETE
    USING (tenant_id = app_current_tenant());
