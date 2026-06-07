-- 120_vt361_tenant_business_verification.sql — VT-361: Option F business-verification result storage.
--
-- Instant business verification (GSTIN lookup + reverse-penny-drop name bind, vendor = Sandbox by
-- Quicko). RESULT ONLY — no documents, no bank/account numbers (reverse penny-drop collects nothing
-- from the owner; the vendor's payer name is matched then discarded). DPDP-light.
--
-- Tiers (Fazal 2026-06-08 — GST-OTP is DEAD, no accessible API, so NO gstin_otp_verified tier):
--   unverified      — default / vendor-down / no match (fail-closed).
--   name_verified   — reverse-penny-drop payer name matches the claimed business name (no GSTIN).
--   gstin_verified  — GSTIN lookup name ∧ reverse-penny-drop payer name both match (top tier).
--
-- Columns inherit the tenants RLS (mig 001 — app_current_tenant() row isolation); no new policy.
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'name_verified', 'gstin_verified')),
    ADD COLUMN IF NOT EXISTS verified_business_name TEXT NULL,  -- authoritative name (lookup or payer match)
    ADD COLUMN IF NOT EXISTS verification_method TEXT NULL,     -- 'gstin_reverse_penny_drop' | 'reverse_penny_drop' | NULL
    ADD COLUMN IF NOT EXISTS gstin TEXT NULL,                   -- the looked-up GSTIN (public id, not a secret)
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ NULL;

-- DSR (VT-361 correction 3): verified_business_name (owner/business PII) + gstin are folded into the
-- tenant-anonymize set in dsr_purge._anonymize_tenant_row (with a test asserting neither survives).

-- Verification attempt log — serves BOTH the per-tenant-per-day attempt cap (no retry storms) AND
-- the wallet-cost category log. Result-only: NO payer names, NO vendor payloads — just the action +
-- outcome tier + a cost bucket. Tenant-scoped RLS (mig 001 GUC pattern).
CREATE TABLE IF NOT EXISTS public.kyc_verification_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action        TEXT NOT NULL CHECK (action IN ('lookup', 'initiate', 'bind')),
    outcome       TEXT NULL,        -- resulting verification_status, or an error tag (no PII)
    cost_category TEXT NOT NULL,    -- 'gstin_search' | 'reverse_penny_drop' (wallet economics)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.kyc_verification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kyc_verification_log FORCE ROW LEVEL SECURITY;
CREATE POLICY kyc_log_select ON public.kyc_verification_log FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY kyc_log_insert ON public.kyc_verification_log FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE INDEX IF NOT EXISTS kyc_verification_log_tenant_day
    ON public.kyc_verification_log (tenant_id, created_at);
