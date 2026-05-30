-- 050_tenant_owner_phone.sql — VT-250 owner-portal OTP auth anchor.
--
-- The owner-portal login flow (Twilio Verify OTP) needs a phone→tenant
-- resolution anchor. Cowork ruling D1 (BINDING): the anchor is a NEW
-- `tenants.owner_phone` column — NOT `whatsapp_number`. Rationale:
--   - whatsapp_number is the BUSINESS number (the WABA sender), which may
--     differ from the owner's personal mobile and is NOT guaranteed unique
--     or owner-personal.
--   - owner_phone is the auth anchor: the personal mobile the owner enters
--     at login. One phone = one tenant for launch (globally unique).
--
-- The uniqueness is GLOBAL (not per-tenant): a normalized E.164 owner_phone
-- maps to exactly one tenant across the whole table. The index is partial
-- (WHERE owner_phone IS NOT NULL) so the many un-set tenants don't collide
-- on NULL.
--
-- E.164 invariant (Cowork D1): owner_phone is stored normalized to E.164 at
-- the write site (onboarding) and looked up normalized at login. The unique
-- index enforces single-tenant resolution; consistent normalization is the
-- app-layer contract that makes the lookup hit.
--
-- CL-422: tenants is tenant-identifying PII — dev holds SYNTHETIC data only
-- until prod-in-Mumbai (VT-231). owner_phone inherits that constraint.
--
-- Migration number: 050 EXACTLY, assigned per CL-424 (047/048/049 are
-- in-flight on other branches; the allocator was NOT run for this row).
-- The runner applies by filename + tracks by name (schema_migrations.name),
-- so 050 merging before 047-049 does NOT skip them — order-independent here
-- (050 has no dependency on 047-049).

-- ADD the owner-phone anchor column (nullable — backfilled/set at onboarding).
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS owner_phone TEXT NULL;

-- GLOBALLY-UNIQUE normalized (E.164) anchor: one owner_phone → one tenant.
-- Partial so unset tenants (NULL) never collide. This is the resolution
-- index the login verify-otp path relies on (phone → exactly one tenant_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_owner_phone_unique
    ON public.tenants (owner_phone)
    WHERE owner_phone IS NOT NULL;
