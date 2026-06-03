-- 087_composition_audits.sql — VT-71 composition audit trail (Pillar 7).
--
-- One row per context-bundle composition (build_sales_recovery_context). Lets
-- ops reconstruct EXACTLY what knowledge the agent saw for a given run: which
-- layers contributed, per-section token counts, which sections truncated under
-- budget pressure, and the L3 cohorts / L4 docs referenced. This is the
-- traceability substrate behind "what informed the agent's decision on run X".
--
-- Composition is NOT a separate module — it lives in context_builder (the one
-- composition function, Pillar 8). This table is the audit it writes.
--
-- Retention: CL-416 LIFETIME-of-relationship (audit/observability substrate),
-- DSR-purge the sole deletion path — tenant_id FK ON DELETE CASCADE handles it.
-- Tenant-scoped RLS (Pillar 3). Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE composition_audits (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id              UUID NOT NULL,
    cohort_key          TEXT,
    -- per-section token estimates (the five content sections + the moat layers).
    section_token_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_token_count   INTEGER NOT NULL DEFAULT 0,
    -- sections trimmed to fit the global budget (the truncation order's effect).
    truncated_sections  TEXT[] NOT NULL DEFAULT '{}',
    -- moat-layer provenance: which L3 cohorts + L4 docs the agent actually saw.
    l3_cohort_keys      TEXT[] NOT NULL DEFAULT '{}',
    l4_doc_ids          UUID[] NOT NULL DEFAULT '{}',
    composed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE composition_audits ENABLE ROW LEVEL SECURITY;
ALTER TABLE composition_audits FORCE ROW LEVEL SECURITY;

CREATE POLICY composition_audits_select ON composition_audits FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY composition_audits_insert ON composition_audits FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());

-- "what did the agent see for run X" + per-tenant recent compositions.
CREATE INDEX composition_audits_run ON composition_audits (run_id);
CREATE INDEX composition_audits_tenant_time
    ON composition_audits (tenant_id, composed_at DESC);
