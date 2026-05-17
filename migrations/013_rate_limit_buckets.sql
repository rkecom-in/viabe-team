-- 013_rate_limit_buckets.sql — per-tenant + workspace inbound rate limiting
-- (VT-3.3b). Fixed-window counters: one row per (tenant_id, minute-window).
--
-- The workspace-wide limit uses the all-zeros sentinel tenant_id. tenant_id
-- has NO foreign key so the sentinel is permitted (and so a deleted tenant's
-- old buckets do not block cleanup).
CREATE TABLE rate_limit_buckets (
    tenant_id    UUID NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count        INT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, window_start)
);

-- For the cleanup job (VT-3.5 scheduled trigger; out of scope here).
CREATE INDEX rate_limit_buckets_window_idx ON rate_limit_buckets (window_start);

-- Pillar 3: tenant-scoped RLS, in the same migration that creates the table.
-- The workspace sentinel row matches no tenant GUC — service-role only.
ALTER TABLE rate_limit_buckets ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_limit_buckets FORCE ROW LEVEL SECURITY;

CREATE POLICY rate_limit_buckets_select ON rate_limit_buckets FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY rate_limit_buckets_insert ON rate_limit_buckets FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY rate_limit_buckets_update ON rate_limit_buckets FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY rate_limit_buckets_delete ON rate_limit_buckets FOR DELETE
    USING (tenant_id = app_current_tenant());
