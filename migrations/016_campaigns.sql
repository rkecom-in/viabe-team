-- 016_campaigns.sql — persisted CampaignPlan instances (VT-3.4 PR 3/3).
--
-- One row per CampaignPlan emitted by a specialist (currently sales_recovery).
-- The collapse path inserts a row at run completion; status flips later via
-- VT-6 (owner approval) and VT-5 (sender). Schema mirrors the CampaignPlan
-- pydantic model (CL-177 locked), plus run_id and created_at.
--
-- Minimal-by-design (CL-233): only the columns the collapse path writes /
-- downstream consumers need. No indexes beyond the PK / FKs until a query
-- pattern materialises.
CREATE TABLE campaigns (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id),
    run_id        UUID NOT NULL REFERENCES pipeline_runs (id),
    subscriber_id UUID NOT NULL,
    template_id   TEXT NOT NULL,
    body_params   JSONB NOT NULL,
    status        TEXT NOT NULL CHECK (status IN (
                      'proposed', 'approved', 'rejected', 'sent', 'failed')),
    proposed_at   TIMESTAMPTZ NOT NULL,
    proposed_by   TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pillar 3: RLS lives in the same migration that creates the table.
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns FORCE ROW LEVEL SECURITY;

CREATE POLICY campaigns_select ON campaigns FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY campaigns_insert ON campaigns FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaigns_update ON campaigns FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaigns_delete ON campaigns FOR DELETE
    USING (tenant_id = app_current_tenant());
