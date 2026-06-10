-- 122_vt366_business_profile_draft.sql — VT-366 Gap-2a Auto-Discovery Engine.
--
-- At signup-complete the engine assembles a DRAFT business profile from public sources (GBP +
-- the business's own website). Public data hallucinates / goes stale, so the draft is
-- owner-CONFIRMED before ANY field is promoted to the canonical `business_profile` (l1_entities)
-- or emitted to the KG. Per-field provenance {source, fetched_at} drives the confirm UI + later
-- staleness refresh. Tenant-scoped business data → RLS in this migration (Pillar 3) AND it MUST be
-- swept by dsr_purge (`business_profile_draft` is in `_PURGE_ORDER`; a hard-delete DSR canary proves
-- it) — a new tenant table forgotten in the purge order is the recurring DSR drift (CL-390).

CREATE TABLE business_profile_draft (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants (id),
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- drafted fields (name/category/city/rating/website/...)
    provenance  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-field {field: {source, fetched_at}}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One draft per tenant; re-discovery MERGES into the same row (idempotent).
CREATE UNIQUE INDEX business_profile_draft_one_per_tenant ON business_profile_draft (tenant_id);

-- Pillar 3: RLS in the same migration that creates the table (mirrors l1_entities). FORCE so table
-- ownership alone cannot bypass RLS — the table is touched by the privileged owner pool (dsr_purge);
-- 000b_rls_helpers.sql invariant: "RLS is FORCED on every table".
ALTER TABLE business_profile_draft ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_profile_draft FORCE ROW LEVEL SECURITY;
CREATE POLICY business_profile_draft_select ON business_profile_draft FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY business_profile_draft_insert ON business_profile_draft FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY business_profile_draft_update ON business_profile_draft FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY business_profile_draft_delete ON business_profile_draft FOR DELETE
    USING (tenant_id = app_current_tenant());
