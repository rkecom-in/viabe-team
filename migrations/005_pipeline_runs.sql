-- 005_pipeline_runs.sql — one row per orchestrator/specialist pipeline run.
--
-- VT-178 docstring amendment (2026-05-26): this table's column shape on main
-- differs from the VT-122 design doc §2.1 specification. Actual columns:
-- `id, tenant_id, run_type, status, started_at, ended_at, trigger_payload (JSONB),
-- terminal_state_metadata (JSONB), cost_paise`. Brief §2.1 names several
-- separate columns (`trigger_kind`, `trigger_source_ref`, `final_outcome`,
-- `step_count`, `error_summary`) that this schema instead carries as JSONB
-- payload fields (`trigger_payload`, `terminal_state_metadata`). Schema
-- normalization to §2.1 spec deferred to VT-187 (Cowork files post-VT-178).
-- VT-178 ships only the composite indexes via `024_pipeline_observability_indexes.sql`.

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
