-- 006_pipeline_steps.sql — ordered steps within a pipeline run.
--
-- NOTE: the VT-122.1 column list reaches the tenant only via run_id. Pillar 3
-- requires `tenant_id NOT NULL` on *every* multi-tenant table, so tenant_id is
-- carried here directly (denormalised from pipeline_runs) — this keeps the RLS
-- policy a direct equality check rather than a subquery into another
-- RLS-protected table.
CREATE TABLE pipeline_steps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES pipeline_runs (id),
    tenant_id       UUID NOT NULL REFERENCES tenants (id),
    step_index      INT NOT NULL,
    step_kind       TEXT,
    input_envelope  JSONB,
    output_envelope JSONB,
    rationale       TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    cost_paise      INT NOT NULL DEFAULT 0,
    duration_ms     INT,
    error_envelope  JSONB
);

CREATE INDEX pipeline_steps_run_idx ON pipeline_steps (run_id);
CREATE INDEX pipeline_steps_tenant_idx ON pipeline_steps (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration.
ALTER TABLE pipeline_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_steps FORCE ROW LEVEL SECURITY;

CREATE POLICY pipeline_steps_select ON pipeline_steps FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY pipeline_steps_insert ON pipeline_steps FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pipeline_steps_update ON pipeline_steps FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pipeline_steps_delete ON pipeline_steps FOR DELETE
    USING (tenant_id = app_current_tenant());
