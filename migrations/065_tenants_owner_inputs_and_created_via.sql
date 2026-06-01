-- 065_tenants_owner_inputs_and_created_via.sql — VT-267 prereq (PR-A).
--
-- F1/D2 (Cowork VT-267 ruling 2026-06-01): tenants.owner_inputs is read by the
-- consent gate (_owner_inputs_enabled, l0_writer.py:31) AND the VT-52 vision /
-- VT-59 voice extraction fail-closed gates — but it was NEVER defined in a tracked
-- migration. It lived only as an untracked Supabase-dashboard column, exactly the
-- drift that bites a fresh-migrated DB (the column is absent → every consent canary
-- errors). Define it here, IF NOT EXISTS-safe so an existing dashboard column + its
-- values are PRESERVED (no clobber). Fail-closed default FALSE: no consent until the
-- owner enables (CL-390 / CL-425).
--
-- created_via: dual-entry provenance (whatsapp | qr | web | owner_login) for VT-267
-- onboarding. The COLUMN is identity-model-independent (D1, Fazal-held); the
-- create-tenant LOGIC that populates it is deferred until Fazal's D1 ruling.
--
-- Migration 065 via scripts/migration_id_allocate.py (CL-424). RLS already on
-- tenants; column adds inherit it. No data backfill needed (defaults apply).

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS owner_inputs BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS created_via TEXT NULL;
