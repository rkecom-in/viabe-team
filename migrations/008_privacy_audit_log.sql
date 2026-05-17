-- 008_privacy_audit_log.sql — hash-chained privacy audit log (DPDP, 7-year
-- retention).
--
-- Each row's this_hash is computed from its payload plus the previous row's
-- hash, forming a tamper-evident chain. tenant_id is NULLABLE: workspace-level
-- privacy events (not tied to a single tenant) carry NULL. The hash-chain
-- computation and append-only enforcement are built in VT-8 — this migration
-- only creates the table.
CREATE TABLE privacy_audit_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID REFERENCES tenants (id),
    event_type TEXT,
    payload    JSONB,
    prev_hash  TEXT,
    this_hash  TEXT NOT NULL,
    event_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT
);

CREATE INDEX privacy_audit_log_tenant_idx ON privacy_audit_log (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration. Rows with NULL tenant_id
-- (workspace events) are invisible to every tenant context and reachable only
-- via the RLS-bypassing service role.
ALTER TABLE privacy_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY privacy_audit_log_select ON privacy_audit_log FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY privacy_audit_log_insert ON privacy_audit_log FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY privacy_audit_log_update ON privacy_audit_log FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY privacy_audit_log_delete ON privacy_audit_log FOR DELETE
    USING (tenant_id = app_current_tenant());
