-- 068_oauth_install_state.sql — VT-289 OAuth callback state-CSRF hardening.
--
-- The single-use, server-side nonce that replaces "state = raw tenant_id, trusted"
-- across every OAuth-install callback (google_sheet, shopify #227, WhatsApp VT-286).
-- `/setup` (authenticated owner context) MINTS a row; the provider redirect carries
-- the opaque `state`; the callback ATOMICALLY claims it (single-use) and derives the
-- tenant_id from THIS row — never from the URL. Defeats the account-linking CSRF where
-- an attacker forges `state=<victim_tenant>` (HIGH item flagged on #227).
--
-- Workspace-wide / service-role-only (like 004_razorpay_webhook_events): the callback
-- has NO tenant GUC (it is identified only by the nonce), and lookup is BY state, not
-- by tenant — so a tenant-scoped policy cannot express the read. RLS is enabled +
-- FORCED with a deny-all policy; only the RLS-bypassing service role (the bare pool /
-- Supabase secret) reaches it. The row holds only an opaque nonce + tenant_id; no PII
-- (CL-390), per-field columns (CL-417). Migration 068 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.oauth_install_state (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state         TEXT NOT NULL UNIQUE,          -- secrets.token_urlsafe(32) nonce
    tenant_id     UUID NOT NULL,
    connector_id  TEXT NOT NULL,                 -- google_sheet | shopify | whatsapp
    target        TEXT,                          -- shop domain / waba context, optional
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ                    -- NULL until claimed; single-use
);

-- expiry sweep helper (a janitor can DELETE WHERE expires_at < now()).
CREATE INDEX IF NOT EXISTS idx_oauth_install_state_expires
    ON public.oauth_install_state (expires_at);

-- Service-role-only: deny-all RLS. Only the BYPASSRLS service role (the bare pool)
-- reaches this — exactly the /setup + /callback handlers, never a tenant client.
ALTER TABLE public.oauth_install_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_install_state FORCE ROW LEVEL SECURITY;

CREATE POLICY oauth_install_state_no_tenant_access ON public.oauth_install_state
    FOR ALL
    USING (false)
    WITH CHECK (false);
