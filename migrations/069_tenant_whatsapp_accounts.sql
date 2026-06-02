-- 069_tenant_whatsapp_accounts.sql — VT-286 owner-owned WABA onboarding.
--
-- Per-tenant WhatsApp Business Account state from Meta Embedded Signup (Twilio
-- tech-provider). Meta mandates client-owned WABAs (On-Behalf-Of is dead); each
-- tenant owns its WABA + a dedicated number. This is a DEDICATED table (not the
-- generic tenant_oauth_tokens) because a WABA carries more identity fields
-- (waba_id, phone_number_id, display name + verification status).
--
-- status state machine (Jan-2026 Meta rule: business verification + privacy URL
-- before templates): pending -> verifying -> name_approved -> live. A tenant
-- CANNOT send until `live` — enforced application-side by `wa_send_allowed`.
--
-- Privacy: the access token is encrypted at rest via the VT-191 Fernet substrate
-- (shared encrypt_value, TEAM_PHONE_ENCRYPTION_KEY). No raw PII (CL-390); per-field
-- columns (CL-417). Tenant-scoped RLS, FORCE (CL-82/88). CL-422 dev = synthetic.
-- Migration 069 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.tenant_whatsapp_accounts (
    tenant_id               UUID PRIMARY KEY REFERENCES tenants(id),
    waba_id                 TEXT,
    phone_number_id         TEXT,
    phone_number            TEXT,            -- dedicated number per tenant
    display_name            TEXT,            -- the shop name shown to customers
    status                  TEXT NOT NULL DEFAULT 'pending',
    access_token_encrypted  TEXT,            -- Fernet; system-user / WABA token
    token_expires_at        TIMESTAMPTZ,     -- NULL = long-lived system-user token
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_whatsapp_accounts_status_chk
        CHECK (status IN ('pending', 'verifying', 'name_approved', 'live'))
);

ALTER TABLE public.tenant_whatsapp_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_whatsapp_accounts FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_whatsapp_accounts_select ON public.tenant_whatsapp_accounts
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_whatsapp_accounts_insert ON public.tenant_whatsapp_accounts
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_whatsapp_accounts_update ON public.tenant_whatsapp_accounts
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_whatsapp_accounts_delete ON public.tenant_whatsapp_accounts
    FOR DELETE USING (tenant_id = app_current_tenant());
