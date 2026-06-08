-- 120_vt361_tenant_business_verification.sql — VT-361: Option F business-verification (two-tier).
--
-- Instant business verification via Sandbox by Quicko GSTIN lookup. RESULT ONLY — no documents,
-- no bank/account data (ownership-bind via penny-drop was CUT — Fazal two-tier ruling 2026-06-08).
-- DPDP-light.
--
-- Tiers (Fazal two-tier ruling 2026-06-08):
--   unverified     — default / vendor-down / GSTIN not-found-or-inactive (fail-closed). Cannot activate.
--   gstin_verified — "yellow": Sandbox search_gstin returned an ACTIVE GSTIN; the authoritative
--                    legal/trade name is stored. Lookup success ALONE earns it (no bind — Fazal's
--                    accepted impersonation residual; VTR backstop). This is the activation-gate tier.
--   vtr_verified   — "green": manual VTR/ops upgrade (audited). NO product significance yet (gates
--                    nothing); value arrives in a later phase.
--
-- ACTIVATION GATE (transitions.py): card_captured → paid_active requires verification_status in
-- (gstin_verified, vtr_verified). GSTIN-less businesses cannot activate — intended.
--
-- Columns inherit the tenants RLS (mig 001 — app_current_tenant() row isolation); no new policy.
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'gstin_verified', 'vtr_verified')),
    ADD COLUMN IF NOT EXISTS verified_business_name TEXT NULL,  -- authoritative name from the GSTIN lookup
    ADD COLUMN IF NOT EXISTS verification_method TEXT NULL,     -- 'gstin_lookup' | 'vtr_override' | NULL
    ADD COLUMN IF NOT EXISTS gstin TEXT NULL,                   -- the looked-up GSTIN (public id, not a secret)
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ NULL;

-- DSR (VT-361): verified_business_name (business/owner PII) + gstin (a strong re-id anchor) are
-- folded into dsr_purge._TENANT_ANONYMIZE (with a test asserting neither survives).

-- Verification attempt log — serves BOTH the per-tenant-per-day attempt cap (no retry storms) AND
-- the wallet-cost category log. Result-only: NO names, NO vendor payloads — just the action + outcome
-- + a cost bucket. `outcome` distinguishes vendor-down from invalid-GSTIN (ops: outage vs bad input).
-- Tenant-scoped RLS (mig 001 GUC pattern).
CREATE TABLE IF NOT EXISTS public.kyc_verification_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action        TEXT NOT NULL CHECK (action IN ('lookup', 'vtr_override')),
    outcome       TEXT NULL,        -- 'gstin_verified' | 'vendor_down' | 'invalid_gstin' | 'vtr_verified' | error tag
    cost_category TEXT NOT NULL,    -- 'gstin_search' | 'none' (override is free) — wallet economics
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.kyc_verification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kyc_verification_log FORCE ROW LEVEL SECURITY;
CREATE POLICY kyc_log_select ON public.kyc_verification_log FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY kyc_log_insert ON public.kyc_verification_log FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE INDEX IF NOT EXISTS kyc_verification_log_tenant_day
    ON public.kyc_verification_log (tenant_id, created_at);
