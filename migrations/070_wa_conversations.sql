-- 070_wa_conversations.sql — VT-287 per-customer inbound conversation state.
--
-- The existing pipeline is owner-centric (subscriber_states is PK(tenant_id) = the
-- owner). Customer-inbound (inbound-first WhatsApp on the owner's WABA) is a different
-- actor: many customers per tenant. This table is the per-customer conversation marker
-- the deterministic customer-inbound path needs — the home for the intro-once re-send
-- guard (intro_sent_at) + 24h-window tracking (last_inbound_at). Cowork ruling 2026-06-02.
--
-- Privacy: phone_token-keyed (CL-390; hash_phone), NO raw PII. Per-field columns
-- (CL-417). Tenant-scoped RLS, FORCE (CL-82/88). CL-422 dev = synthetic.
-- Migration 070 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.wa_conversations (
    tenant_id        UUID NOT NULL,
    phone_token      TEXT NOT NULL,
    intro_sent_at    TIMESTAMPTZ,     -- set once when the first-contact intro is sent
    last_inbound_at  TIMESTAMPTZ,     -- most recent customer inbound (24h-window tracking)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, phone_token)
);

ALTER TABLE public.wa_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wa_conversations FORCE ROW LEVEL SECURITY;

CREATE POLICY wa_conversations_select ON public.wa_conversations
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY wa_conversations_insert ON public.wa_conversations
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY wa_conversations_update ON public.wa_conversations
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY wa_conversations_delete ON public.wa_conversations
    FOR DELETE USING (tenant_id = app_current_tenant());
