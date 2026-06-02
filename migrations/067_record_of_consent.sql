-- 067_record_of_consent.sql — VT-8.5 customer consent-capture surface.
--
-- The proof-of-consent record for the QR opt-in: a customer who scans a
-- business's QR and accepts the terms gets ONE row per (tenant, phone_token).
-- This is the privacy half of the QR flow; the clean-ledger half (writing the
-- customer + first message) is VT-60, which fail-CLOSED gates on an active row
-- here before any customer write/message.
--
-- Identity-precedes-customer: a QR scan can happen BEFORE a customers row
-- exists, so there is deliberately NO FK to customers. The phone_token is the
-- join key; VT-60 resolves token -> customer at write time.
--
-- Privacy:
--   * NO raw PII (CL-390): phone is tokenised via utils.phone_token.hash_phone
--     (phone_tok_<sha256>) at the application boundary; the raw E.164 number is
--     NEVER stored here. Resolve-back, if ever needed, goes through the
--     phone_token_resolutions encrypted seam — not this surface.
--   * CL-417 per-field columns; NO JSONB.
--   * consent_text_version stores the VERSION STRING only; the copy + locale
--     text live single-sourced in .viabe/consent-text.md (Cowork drafts, Fazal
--     legal-validates — RKeCom Services OPC Pvt Ltd).
--
-- Re-consent (Fix 1, Cowork VT-85 review 2026-06-02): a previously opted-out
-- customer who re-consents has opted_out_at RESET to NULL on the same row (the
-- application UPSERT does this) — otherwise they stay permanently blocked.
--
-- Pillar 3: RLS in the same migration, FORCE (CL-82/88). app_role DML comes
-- from the migration-015 default privileges (table created after 015, same as
-- 061/062/063). CL-422 dev = synthetic only. Migration 067 via the allocator
-- (CL-424; 066 reserved for the in-flight VT-267 PR-A2 / PR #224).

CREATE TABLE IF NOT EXISTS public.record_of_consent (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL,
    phone_token          TEXT NOT NULL,
    consent_text_version TEXT NOT NULL,
    consent_method       TEXT NOT NULL DEFAULT 'qr_optin',
    source               TEXT,
    locale               TEXT,
    consented_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = active consent; non-NULL = consent withdrawn (opt-out).
    opted_out_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one consent record per customer per tenant; re-consent is idempotent
    -- (UPSERT updates version + clears opted_out_at on the same row).
    CONSTRAINT record_of_consent_idem UNIQUE (tenant_id, phone_token)
);

CREATE INDEX IF NOT EXISTS idx_record_of_consent_tenant
    ON public.record_of_consent (tenant_id);

ALTER TABLE public.record_of_consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.record_of_consent FORCE ROW LEVEL SECURITY;

CREATE POLICY record_of_consent_select ON public.record_of_consent
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY record_of_consent_insert ON public.record_of_consent
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY record_of_consent_update ON public.record_of_consent
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY record_of_consent_delete ON public.record_of_consent
    FOR DELETE USING (tenant_id = app_current_tenant());
