-- 049_outbound_send_ledger.sql — VT-44 outbound-send ledger.
--
-- Two tables owned by VT-44; VT-45 (send_whatsapp_template) REUSES both.
--
-- send_idempotency_keys: one row per send attempt; idempotent on
--   (tenant_id, idempotency_key). Doubles as the outbound-send audit
--   record (carries message_sid, send_status, customer_id). No separate
--   campaign_messages table for VT-44; the ledger row IS the per-send
--   record (Decision D2 — approved by plan-ready verdict).
--
-- campaign_messages: shallow per-message record owned here so VT-45 can
--   write it without a second migration. Nullable foreign keys allow
--   freeform (VT-44) sends that have no campaign context.
--
-- Pillar 3: RLS lives in the same migration (CL-82 GUC convention via
--   app_current_tenant()).
-- CL-422: dev holds synthetic data only until prod-in-Mumbai (VT-231).
-- Migration number: 049, allocated per CL-424 (counter was 49 pre-build;
--   .next-migration bumped to 50 below).

-- =================== send_idempotency_keys ============================

CREATE TABLE IF NOT EXISTS public.send_idempotency_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    customer_id     UUID NULL,      -- recipient (nullable: pre-resolve failures)
    message_sid     TEXT NULL,      -- Twilio SID on success
    send_status     TEXT NOT NULL CHECK (send_status IN (
                        'sent', 'window_closed', 'rate_limited', 'error')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: a second identical key returns the prior row, no re-send.
    CONSTRAINT send_idempotency_keys_tenant_key_uniq
        UNIQUE (tenant_id, idempotency_key)
);

-- per-customer 6h anti-spam lookup + per-tenant daily cap lookup:
CREATE INDEX IF NOT EXISTS idx_send_idem_tenant_customer_created
    ON public.send_idempotency_keys (tenant_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_send_idem_tenant_created
    ON public.send_idempotency_keys (tenant_id, created_at);

ALTER TABLE public.send_idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.send_idempotency_keys FORCE ROW LEVEL SECURITY;

CREATE POLICY send_idempotency_keys_select ON public.send_idempotency_keys
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY send_idempotency_keys_insert ON public.send_idempotency_keys
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY send_idempotency_keys_update ON public.send_idempotency_keys
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY send_idempotency_keys_delete ON public.send_idempotency_keys
    FOR DELETE USING (tenant_id = app_current_tenant());

-- =================== campaign_messages ================================
-- Per-message outbound record. Nullable campaign_id = freeform send (VT-44).
-- A same-tenant composite FK (tenant_id, campaign_id) -> campaigns(tenant_id, id)
-- mirrors the 045 cohort-integrity pattern (campaigns_tenant_id_uniq enables it):
-- MATCH SIMPLE means the FK is enforced ONLY when campaign_id is set, so freeform
-- sends (campaign_id NULL) skip it while VT-45 template sends get real referential
-- integrity + cross-tenant-link prevention. customer_id is intentionally NOT FK'd:
-- this is an append-only audit ledger, so a send record must survive customer
-- deletion (per Cowork review).

CREATE TABLE IF NOT EXISTS public.campaign_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    customer_id     UUID NULL,
    campaign_id     UUID NULL,      -- NULL for freeform sends (VT-44); set by VT-45
    idempotency_key TEXT NULL,      -- mirrors send_idempotency_keys.idempotency_key
    message_sid     TEXT NULL,      -- Twilio SID on success
    send_status     TEXT NOT NULL CHECK (send_status IN (
                        'sent', 'window_closed', 'rate_limited', 'error',
                        'template_sent')),
    message_type    TEXT NOT NULL DEFAULT 'freeform'
                    CHECK (message_type IN ('freeform', 'template')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Same-tenant composite FK: enforced only when campaign_id is set
    -- (MATCH SIMPLE); freeform sends (NULL) are exempt.
    CONSTRAINT campaign_messages_campaign_fk
        FOREIGN KEY (tenant_id, campaign_id)
        REFERENCES public.campaigns (tenant_id, id) ON DELETE CASCADE
);

-- Lookup index: per-campaign messages + per-tenant audit window.
CREATE INDEX IF NOT EXISTS idx_campaign_messages_tenant_campaign
    ON public.campaign_messages (tenant_id, campaign_id)
    WHERE campaign_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_campaign_messages_tenant_created
    ON public.campaign_messages (tenant_id, created_at);

ALTER TABLE public.campaign_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaign_messages FORCE ROW LEVEL SECURITY;

CREATE POLICY campaign_messages_select ON public.campaign_messages
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY campaign_messages_insert ON public.campaign_messages
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaign_messages_update ON public.campaign_messages
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaign_messages_delete ON public.campaign_messages
    FOR DELETE USING (tenant_id = app_current_tenant());
