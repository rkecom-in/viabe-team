-- 002_phase_transitions.sql — append-only log of tenant lifecycle phase changes.
CREATE TABLE phase_transitions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id),
    from_phase    TEXT,
    to_phase      TEXT NOT NULL,
    event         TEXT,
    transition_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason        TEXT,
    run_id        UUID
);

CREATE INDEX phase_transitions_tenant_idx ON phase_transitions (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration.
ALTER TABLE phase_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE phase_transitions FORCE ROW LEVEL SECURITY;

CREATE POLICY phase_transitions_select ON phase_transitions FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY phase_transitions_insert ON phase_transitions FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY phase_transitions_update ON phase_transitions FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY phase_transitions_delete ON phase_transitions FOR DELETE
    USING (tenant_id = app_current_tenant());
