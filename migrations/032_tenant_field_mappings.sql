-- 032_tenant_field_mappings.sql — VT-209 field-mapping persistence.
--
-- Per-tenant, per-connector, per-source-field mapping decision. UPSERT
-- on owner correction. Composite PK (tenant_id, connector_id,
-- source_field) means owner re-mapping the same column updates the row
-- in place; the agent reads the latest decision on subsequent sample
-- pulls so it doesn't re-ask. The `decided_by` enum captures lineage
-- (heuristic vs llm vs owner override).
--
-- Per CL-19: typed columns (no JSONB blob for the mapping decision).
-- Per CL-71: tenant-scoped RLS.
-- Per CL-417: canonical per-field columns.
-- Per CL-416: no delete path; DSR-purge owns deletion.

CREATE TABLE IF NOT EXISTS public.tenant_field_mappings (
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    connector_id     TEXT NOT NULL,
    source_field     TEXT NOT NULL,
    canonical_field  TEXT NOT NULL,
    confidence       REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    decided_by       TEXT NOT NULL CHECK (decided_by IN ('heuristic','llm','owner')),
    decided_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connector_id, source_field)
);

CREATE INDEX IF NOT EXISTS idx_tenant_field_mappings_connector
    ON public.tenant_field_mappings (tenant_id, connector_id);

ALTER TABLE public.tenant_field_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_field_mappings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_field_mappings_select ON public.tenant_field_mappings;
CREATE POLICY tenant_field_mappings_select ON public.tenant_field_mappings
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_field_mappings_insert ON public.tenant_field_mappings;
CREATE POLICY tenant_field_mappings_insert ON public.tenant_field_mappings
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_field_mappings_update ON public.tenant_field_mappings;
CREATE POLICY tenant_field_mappings_update ON public.tenant_field_mappings
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_field_mappings_delete ON public.tenant_field_mappings;
CREATE POLICY tenant_field_mappings_delete ON public.tenant_field_mappings
    FOR DELETE USING (tenant_id = app_current_tenant());

-- Operator-claim SELECT (mirrors migration 030/031 pattern).
DROP POLICY IF EXISTS tenant_field_mappings_operator_select ON public.tenant_field_mappings;
CREATE POLICY tenant_field_mappings_operator_select ON public.tenant_field_mappings
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE public.tenant_field_mappings IS
    'VT-209 field-mapping persistence. Per-tenant, per-connector, per-source-field. UPSERT on owner correction.';
