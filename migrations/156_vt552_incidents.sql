-- 156_vt552_incidents.sql — VT-552 (B1 part-2b): the durable incident record + escalation ladder.
--
-- Runs today end and are gone: pipeline_runs carries status + final_outcome, but a run that
-- terminated WITHOUT a definitive outcome and WITHOUT contacting the owner (a "silent terminal")
-- left no durable, escalatable record — the owner never hears, ops never sees. This table is the
-- durable incident spine + the owner→VTR escalation ladder.
--
--   * ONE incident per (run, kind) — idempotent detection (the detector may re-run).
--   * escalation_tier = the LADDER: 0=detected → 1=owner-contacted → 2=vtr-escalated. Advanced by
--     incident_store.escalate_incident (CAS on version). At the VTR tier an escalations row (mig 073,
--     the VTR queue) is created, idempotent via its uq_escalations_run.
--   * vtr_escalation_id = soft pointer to that escalations row (no FK — escalations is a deny-all
--     ops table in a different RLS regime).
--
-- Tenant-scoped run data → RLS + FORCE + operator SELECT (the VTR reads via operator_claim) + in
-- dsr_purge (same PR). run_id is a soft pointer (no FK) — order-insensitive purge.

CREATE TABLE incidents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id            UUID NULL,                    -- soft ref → pipeline_runs (no FK)
    incident_kind     TEXT NOT NULL CHECK (incident_kind IN (
                          'silent_terminal', 'failed_run', 'owner_unreachable', 'other')),
    severity          TEXT NOT NULL DEFAULT 'warning'
                          CHECK (severity IN ('info', 'warning', 'critical')),
    status            TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'escalated', 'resolved', 'cancelled')),
    escalation_tier   INT NOT NULL DEFAULT 0,       -- 0 detected · 1 owner-contacted · 2 vtr-escalated
    owner_contacted   BOOLEAN NOT NULL DEFAULT false,
    vtr_escalation_id UUID NULL,                    -- soft ref → escalations (mig 073)
    detail            JSONB NULL,                    -- REDACTED context
    version           INT NOT NULL DEFAULT 1,        -- CAS guard
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotent detection: one incident per (run, kind).
CREATE UNIQUE INDEX incidents_run_kind
    ON incidents (run_id, incident_kind) WHERE run_id IS NOT NULL;
CREATE INDEX incidents_tenant_open
    ON incidents (tenant_id, status) WHERE status <> 'resolved';

ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE incidents FORCE ROW LEVEL SECURITY;
CREATE POLICY incidents_select ON incidents FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY incidents_insert ON incidents FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY incidents_update ON incidents FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY incidents_delete ON incidents FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE POLICY incidents_operator_select ON incidents
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
