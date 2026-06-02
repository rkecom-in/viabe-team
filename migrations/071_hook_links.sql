-- 071_hook_links.sql — VT-288 durable hook→WhatsApp attribution links.
--
-- Email/SMS hooks drive customers to message the business on WhatsApp (feeds the
-- VT-287 inbound-first pipeline). The hook carries a SHORT TOKENISED link we own
-- (`/r/<token>`); the redirect resolves token→tenant SERVER-SIDE and 302s to the
-- tenant's WABA wa.me. Attribution is the server-side token mapping + click record —
-- it does NOT rely on the user-editable `wa.me?text=` payload (Cowork VT-288 gotcha).
--
-- Service-role-only (deny-all RLS, like 068_oauth_install_state / 004_razorpay): the
-- `/r/<token>` redirect is PUBLIC and has NO tenant GUC — it resolves BY token, so a
-- tenant-scoped policy can't express the read. The bare service pool is the sole access
-- path; the token IS the capability. No PII (CL-390); per-field columns (CL-417).
-- Migration 071 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.hook_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token           TEXT NOT NULL UNIQUE,        -- secrets.token_urlsafe; the /r/<token>
    tenant_id       UUID NOT NULL,
    source          TEXT,                         -- campaign / channel tag (sms|email|...)
    click_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_clicked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hook_links_tenant ON public.hook_links (tenant_id);

ALTER TABLE public.hook_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.hook_links FORCE ROW LEVEL SECURITY;

CREATE POLICY hook_links_no_tenant_access ON public.hook_links
    FOR ALL
    USING (false)
    WITH CHECK (false);
