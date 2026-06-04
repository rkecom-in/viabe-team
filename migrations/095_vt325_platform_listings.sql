-- VT-325 — platform_listings: the per-listing platform SOURCE.
--
-- Distinct from VT-6 `business_profile` (the per-tenant AGGREGATE merged into one
-- l1_entities JSONB): this holds ONE row per (tenant, platform, external_listing_id)
-- for the tenant's OWN listings. Multi-outlet SMBs have multiple per platform
-- (two branches → two Swiggy listings), so the UNIQUE is the 3-tuple, not platform.
--
-- Feeds the `platform_listing_updated` outbox event (VT-65) → the existing
-- `_h_platform_listing_updated` KG consumer (PLATFORM_LISTING node + HAS_LISTING
-- edge). VT-308 later adds consumer-side HAS_THEME edges off the structured attrs.
--
-- Tenant-scoped HOT table: RLS ENABLE + FORCE + app_current_tenant() policies, and
-- accessed ONLY through PlatformListingsWrapper (VT-306). app_role gets DML via the
-- migration-015 ALTER DEFAULT PRIVILEGES (no explicit GRANT needed, like customers).
--
-- CL-390: `attributes` holds ONLY structured, non-PII listing facts
-- (name/category/cuisines/hours/items). NEVER raw review snippets or customer text
-- at rest.

CREATE TABLE IF NOT EXISTS public.platform_listings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    platform            TEXT NOT NULL
                        CHECK (platform IN ('gbp', 'swiggy', 'zomato')),
    external_listing_id TEXT NOT NULL,
    rating              NUMERIC NULL,
    attributes          JSONB NOT NULL DEFAULT '{}',
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT platform_listings_tenant_platform_extid_uniq
        UNIQUE (tenant_id, platform, external_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_platform_listings_tenant
    ON public.platform_listings (tenant_id, platform);

ALTER TABLE public.platform_listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_listings FORCE ROW LEVEL SECURITY;

CREATE POLICY platform_listings_select ON public.platform_listings
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY platform_listings_insert ON public.platform_listings
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY platform_listings_update ON public.platform_listings
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY platform_listings_delete ON public.platform_listings
    FOR DELETE USING (tenant_id = app_current_tenant());
