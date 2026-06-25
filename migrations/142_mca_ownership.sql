-- 142_mca_ownership.sql — VT-449 / VT-411: MCA company-master + tier-2 ownership persistence.
--
-- The persistence + privacy layer for the Sandbox MCA bundle (apps/team-orchestrator
-- integrations/methods/mca.py). Company Master Data validates + enriches a business from
-- its CIN (canonical name, status/compliance, financials, registered address, directors);
-- the VT-411 tier-2 ownership signal is the owner-channel verification flag on tenants.
--
-- Two surfaces:
--   1) tenants.owner_channel_verified (+ _at) — VT-411 tier-2 ownership, layered ON TOP OF
--      verification_status (mig 120). verification_status answers "is this a real, GSTIN-active
--      business?"; owner_channel_verified answers "did THIS owner prove control of the owner
--      channel?" (the KYC-grade ownership bind). Both are needed; neither subsumes the other.
--   2) tenant_mca_data — the parsed CompanyMasterData at rest, ONE row per tenant (UPSERT).
--
-- PRIVACY (BINDING — CL-390/425/426/104, and mca.py's module docstring):
--   * directors[] (name + din) and the registered_address are PERSONAL/identifying data.
--     They are stored as CIPHERTEXT ONLY — registered_address_encrypted / directors_encrypted
--     hold Fernet output (orchestrator.observability.encrypt_value); the plaintext NEVER lands
--     in a column, a log, or an LLM prompt.
--   * The company financials/status/class/category/roc/incorporation/cin are NON-PII registry
--     facts — stored plain (they are the enrichment payload the agent reads).
--   * DSR: tenant_mca_data is folded into dsr_purge._PURGE_ORDER (a per-tenant leaf table,
--     swept before the tenants anonymize) so a tenant DSR-delete erases the encrypted PII.
--     The two new tenants columns inherit the tenants RLS + DSR anonymize path (mig 001 / 120).
--
-- Columns inherit the tenants RLS (mig 001 — app_current_tenant() row isolation); no new policy
-- needed for the ALTER. The new table gets its own FORCE-RLS tenant-scoped policy set (mig 120
-- kyc_verification_log / mig 067 record_of_consent pattern). Migration 142 via the allocator
-- (CL-424). CL-422 dev = synthetic only.

-- 1) VT-411 tier-2 ownership flag (on top of verification_status).
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS owner_channel_verified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS owner_channel_verified_at TIMESTAMPTZ NULL;

-- 2) Parsed MCA Company Master Data at rest — ONE row per tenant (UPSERT on tenant_id).
-- PII columns (registered_address, directors) hold CIPHERTEXT; the rest are plain registry facts.
CREATE TABLE IF NOT EXISTS public.tenant_mca_data (
    id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                     UUID NOT NULL,
    cin                           TEXT,
    company_name                  TEXT,
    status                        TEXT,
    active_compliance             TEXT,
    class_of_company              TEXT,
    company_category              TEXT,
    roc_code                      TEXT,
    date_of_incorporation         TEXT,
    paid_up_capital               TEXT,
    authorised_capital            TEXT,
    registered_address_encrypted  TEXT,   -- PII — Fernet ciphertext (encrypt_value), never plaintext
    directors_encrypted           TEXT,   -- PII — Fernet ciphertext of json.dumps(list(directors))
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_mca_data_tenant_uniq UNIQUE (tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_mca_data_tenant
    ON public.tenant_mca_data (tenant_id);

ALTER TABLE public.tenant_mca_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_mca_data FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_mca_data_select ON public.tenant_mca_data
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_mca_data_insert ON public.tenant_mca_data
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_mca_data_update ON public.tenant_mca_data
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
