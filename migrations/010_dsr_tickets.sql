-- 010_dsr_tickets.sql — DPDP data-subject-request tickets (VT-3.8 dsr_handler).
CREATE TABLE dsr_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants (id),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_type    TEXT NOT NULL CHECK (request_type IN ('deletion', 'access', 'correction')),
    status          TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'completed')),
    acknowledged_at TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX dsr_tickets_tenant_idx ON dsr_tickets (tenant_id);

-- Pillar 3: tenant-scoped RLS, in the same migration that creates the table.
ALTER TABLE dsr_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE dsr_tickets FORCE ROW LEVEL SECURITY;

CREATE POLICY dsr_tickets_select ON dsr_tickets FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY dsr_tickets_insert ON dsr_tickets FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY dsr_tickets_update ON dsr_tickets FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY dsr_tickets_delete ON dsr_tickets FOR DELETE
    USING (tenant_id = app_current_tenant());
