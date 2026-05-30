-- 045_customers.sql — VT-170 foundational customer entity + cohort integrity.
--
-- "None of our actions can complete without a customers table" (Fazal
-- 2026-05-30). The SR-Agent's business actions depend on a defined
-- customers dataset:
--   - VT-44 send_whatsapp_message needs customers.last_inbound_at (24h window)
--   - consent gating needs customers.opt_out_status
--   - VT-43 cohort_size/attribution_rate need cohort referential integrity
--   - VT-104 redactor name_registry needs a backing store
--
-- Pillar 3: RLS lives in the same migration that creates the table.
-- CL-104 lineage: customer_id columns on attributions / phone_token_
--   resolutions / campaigns cohort were NULLABLE pending this table.
-- CL-422: customers is tenant-identifying PII — dev holds SYNTHETIC data
--   only until prod-in-Mumbai (VT-231).
--
-- Migration number: legacy row said 023 (taken by attributions). 044 is
-- VT-48 scheduled_followups (held PR #145). This is 045. The runner
-- applies by filename + tracks by name (schema_migrations.name), so 045
-- merging before 044 does NOT skip 044 — order-independent here (045 has
-- no dependency on 044).

-- ============================ customers ===============================

CREATE TABLE IF NOT EXISTS public.customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    display_name    TEXT,
    phone_e164      TEXT NULL,
    email           TEXT NULL,
    last_inbound_at TIMESTAMPTZ NULL,
    opt_out_status  TEXT NOT NULL DEFAULT 'subscribed'
                    CHECK (opt_out_status IN ('subscribed', 'opted_out', 'blocked')),
    source          TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FK target for campaign_recipients' same-tenant composite FK.
    CONSTRAINT customers_tenant_id_uniq UNIQUE (tenant_id, id)
);

-- Partial unique indexes: a tenant can't have two customers with the same
-- phone / email, but NULLs are allowed many times (unknown contact).
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_tenant_phone
    ON public.customers (tenant_id, phone_e164)
    WHERE phone_e164 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_tenant_email
    ON public.customers (tenant_id, email)
    WHERE email IS NOT NULL;
-- 24h-window lookup for VT-44 (most-recent inbound per tenant).
CREATE INDEX IF NOT EXISTS idx_customers_tenant_last_inbound
    ON public.customers (tenant_id, last_inbound_at);

ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customers FORCE ROW LEVEL SECURITY;

CREATE POLICY customers_select ON public.customers
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY customers_insert ON public.customers
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customers_update ON public.customers
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customers_delete ON public.customers
    FOR DELETE USING (tenant_id = app_current_tenant());

-- ===================== cohort integrity ==============================
-- Normalized campaign↔customer linkage. The cohort lives in
-- campaigns.plan_json.target_cohort.customer_ids (mig 018, JSONB) — not
-- joinable, not referentially sound. This table makes cohort_size a real
-- joinable COUNT and makes cross-tenant cohort linkage PHYSICALLY
-- IMPOSSIBLE via same-tenant composite FKs.

-- campaigns needs a (tenant_id, id) unique to be a composite-FK target.
ALTER TABLE public.campaigns
    ADD CONSTRAINT campaigns_tenant_id_uniq UNIQUE (tenant_id, id);

CREATE TABLE IF NOT EXISTS public.campaign_recipients (
    campaign_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    tenant_id   UUID NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, customer_id),
    -- Same-tenant referential integrity: both FKs carry tenant_id, so a
    -- recipient can only link a customer + campaign that share its tenant.
    -- Cross-tenant linkage is rejected by the FK, not by app trust.
    CONSTRAINT campaign_recipients_customer_fk
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES public.customers (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT campaign_recipients_campaign_fk
        FOREIGN KEY (tenant_id, campaign_id)
        REFERENCES public.campaigns (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_campaign_recipients_campaign
    ON public.campaign_recipients (campaign_id);

ALTER TABLE public.campaign_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaign_recipients FORCE ROW LEVEL SECURITY;

CREATE POLICY campaign_recipients_select ON public.campaign_recipients
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY campaign_recipients_insert ON public.campaign_recipients
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaign_recipients_update ON public.campaign_recipients
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY campaign_recipients_delete ON public.campaign_recipients
    FOR DELETE USING (tenant_id = app_current_tenant());
