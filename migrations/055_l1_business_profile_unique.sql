-- 055_l1_business_profile_unique.sql — VT-195 Phase 3.
--
-- L1 identity is ONE 'business_profile' entity per tenant (the always-inject
-- block; VT-195 Phase 1/2). Enforce that invariant + enable a clean idempotent
-- upsert (upsert_business_profile / the RKeCom seed) via a PARTIAL UNIQUE index
-- on (tenant_id) WHERE entity_type = 'business_profile'. Other entity_types
-- (customer, product, ...) keep their many-per-tenant cardinality — the partial
-- predicate scopes the constraint to business_profile only.
--
-- ON CONFLICT targets this index by its predicate, so the writer upserts the
-- single profile row idempotently (re-runs safe). RLS (mig 019) unchanged.

CREATE UNIQUE INDEX IF NOT EXISTS l1_entities_one_business_profile_per_tenant
    ON public.l1_entities (tenant_id)
    WHERE entity_type = 'business_profile';
