-- 051_tenant_recovery_target.sql — VT-164 per-tenant attribution
-- recovery-target config. Replaces the inline *1.1 multiplier +
-- paise (50_000 paise) floor in context_builder.serialize_bundle_for_prompt.
--
-- Backfill = column DEFAULTs: every existing tenant gets exactly the
-- current 1.1 / 50_000 behaviour, so NO behavioural change until a
-- tenant overrides (brief requirement).
--
-- CL-422: tenants holds tenant-identifying data; dev is SYNTHETIC-only
--   until prod-in-Mumbai (VT-231). No real customer data added here.
-- RLS: tenants already has GUC-based RLS (001_tenants.sql, id =
--   app_current_tenant()). Adding columns does not change policies.
-- Migration number 051 assigned up-front (CL-424); 047-050 in flight.
--   Runner tracks by schema_migrations.name, so order is independent.

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS recovery_target_multiplier NUMERIC
        NOT NULL DEFAULT 1.1
        CHECK (recovery_target_multiplier > 0),
    ADD COLUMN IF NOT EXISTS recovery_target_floor_paise BIGINT
        NOT NULL DEFAULT 50000
        CHECK (recovery_target_floor_paise >= 0);
