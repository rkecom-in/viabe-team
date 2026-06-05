-- 105_vt84_owner_excluded_opt_out.sql — VT-84: owner-side per-customer exclusion.
--
-- 'owner_excluded' = the OWNER asked us to skip this customer (Pillar 7, reversible) —
-- DISTINCT from 'opted_out' (the CONSUMER's legal opt-out, VT-8.5). Consumer 'opted_out'
-- ALWAYS takes precedence; clearing owner_excluded must NEVER un-skip a consumer who
-- opted out. Campaign-selection skips BOTH. The reconstitution sweep (VT-76) must NOT
-- treat owner_excluded as a consumer opt-out (Cowork Q3).
ALTER TABLE public.customers DROP CONSTRAINT IF EXISTS customers_opt_out_status_check;
ALTER TABLE public.customers ADD CONSTRAINT customers_opt_out_status_check
    CHECK (opt_out_status IN ('subscribed', 'opted_out', 'blocked', 'owner_excluded'));
