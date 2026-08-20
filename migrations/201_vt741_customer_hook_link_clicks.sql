-- VT-741 — customer-attributed CLICK tracking for the existing /r/<token> link scheme.
--
-- WHY THIS EXISTS
-- ---------------
-- Fazal's re-specified frequency rule (2026-08-13) is RECENCY-based, and two of its three tiers
-- need a CLICK signal:
--     Tier A: replied or clicked within 30 days       -> 24h
--     Tier B: read/clicked/replied within 90 days     -> 3 days
--     Tier C: everyone else                           -> 7 days
-- `read` landed in migration 200. `replied` already exists (wa_conversations.last_inbound_at).
-- `clicked` did NOT exist for customer messages at all — hook_links (mig 071, VT-288) records a
-- click, but only against a TENANT: there is no customer on the row, so a click can never be
-- attributed to the recipient whose interval we are trying to resolve.
--
-- NOT A SECOND LINK SCHEME
-- ------------------------
-- This does NOT mint its own URL space. The public surface stays exactly `/r/<token>`, served by
-- the one redirect (`orchestrator/api/hook_links.py::hook_redirect`), and every token still lives
-- in `hook_links`. This table is a per-customer BINDING hung off that same token, plus the click
-- state for it. A customer-bound link is a `hook_links` row AND a `customer_hook_links` row sharing
-- one token; an unbound (tenant-wide email/SMS) hook is a `hook_links` row alone, unchanged.
--
-- THE TOKEN IS STILL THE CAPABILITY
-- ---------------------------------
-- No customer id, no tenant id, no campaign id and no phone appears in the URL. The redirect
-- resolves token -> tenant -> (optionally) customer entirely server-side, exactly as VT-288 does
-- today. Nothing about the customer is inferable from the link text, so the attribution cannot be
-- forged or enumerated by editing the URL — which is the same property that made the VT-288
-- `wa.me?text=` payload unusable for attribution.
--
-- DIFFERENT PRIVACY CLASS FROM hook_links — HENCE ITS OWN RLS
-- -----------------------------------------------------------
-- `hook_links` is deny-all RLS and carries no PII BY DESIGN: a token and a tenant. This table
-- links a token to an identified END CUSTOMER and records their behaviour, which is subject data.
-- It therefore gets the TENANT-SCOPED four-policy RLS + FORCE that every other customer-linked
-- table carries (mirrors agent_customer_contacts, mig 127) rather than inheriting hook_links'
-- deny-all posture. Deny-all would have been the wrong copy: it would leave the tenant unable to
-- see their own engagement data while doing nothing extra for the customer.
--
-- The PUBLIC redirect has no tenant GUC (it resolves BY token), so the click UPDATE runs on the
-- bare service pool with an explicit `WHERE token = ...` — identical to how hook_links is written
-- today. The tenant policies below serve the owner-facing/read side.
--
-- DSR (the part that has now been missed three times)
-- ---------------------------------------------------
-- `dsr_purge` ANONYMIZES the tenants row, it does not delete it, so NO `ON DELETE CASCADE` from
-- tenants ever fires on a right-to-erasure. Both FKs below are composite ones to
-- customers/hook_links, and `customers` is itself not purged (see the note in the VT-741 report),
-- so there is no cascade path here at all. `customer_hook_links` is registered in
-- `dsr_purge._PURGE_ORDER` in this same change. This omission already shipped twice
-- (episodic_events, then the L2 surfaces); it is not shipping a third time.
--
-- Migration 201 — pre-allocated for VT-741 (CL-424: never hand-picked).

-- ---------------------------------------------------------------------------
-- 1. hook_links gains a composite-unique so it can be a same-tenant FK target.
--    `token` is already globally UNIQUE; this adds (tenant_id, token) so the binding below can
--    carry tenant_id in its FK. Cross-tenant binding then becomes PHYSICALLY impossible rather
--    than a thing application code has to remember (the mig 045 campaign_recipients pattern).
-- ---------------------------------------------------------------------------
ALTER TABLE public.hook_links
    ADD CONSTRAINT hook_links_tenant_token_uniq UNIQUE (tenant_id, token);

-- ---------------------------------------------------------------------------
-- 2. The customer-attributed binding + click state.
-- ---------------------------------------------------------------------------
CREATE TABLE public.customer_hook_links (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL,
    customer_id      UUID NOT NULL,
    token            TEXT NOT NULL UNIQUE,   -- the SAME token as the hook_links row
    source           TEXT,                    -- campaign / channel tag, mirrors hook_links.source
    click_count      INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_clicked_at TIMESTAMPTZ,
    last_clicked_at  TIMESTAMPTZ,             -- the recency anchor Tier A / Tier B read
    -- Same-tenant referential integrity on BOTH sides: a binding can only ever join a token and a
    -- customer that already share its tenant.
    CONSTRAINT customer_hook_links_hook_fk
        FOREIGN KEY (tenant_id, token)
        REFERENCES public.hook_links (tenant_id, token) ON DELETE CASCADE,
    CONSTRAINT customer_hook_links_customer_fk
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES public.customers (tenant_id, id) ON DELETE CASCADE
);

-- The tier lookup: newest click for one (tenant, customer). DESC NULLS LAST because a minted-but-
-- never-clicked binding is the common row and must not sit at the head of the scan.
CREATE INDEX customer_hook_links_customer_recent
    ON public.customer_hook_links (tenant_id, customer_id, last_clicked_at DESC NULLS LAST);

ALTER TABLE public.customer_hook_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customer_hook_links FORCE ROW LEVEL SECURITY;

CREATE POLICY customer_hook_links_select ON public.customer_hook_links FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY customer_hook_links_insert ON public.customer_hook_links FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customer_hook_links_update ON public.customer_hook_links FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customer_hook_links_delete ON public.customer_hook_links FOR DELETE
    USING (tenant_id = app_current_tenant());

COMMENT ON TABLE public.customer_hook_links IS
    'VT-741: per-customer binding for a /r/<token> hook link + its click state. Same token space '
    'as hook_links (no second link scheme); the token remains the sole capability — no customer, '
    'tenant, campaign or phone appears in the URL. Customer-attributed behaviour = subject data, '
    'so this carries tenant-scoped RLS + FORCE (NOT hook_links deny-all) and is registered in '
    'dsr_purge._PURGE_ORDER (the tenants row is anonymized, so no FK cascade would ever clean it).';

COMMENT ON COLUMN public.customer_hook_links.last_clicked_at IS
    'VT-741 recency anchor for the frequency tiers: Tier A = clicked within 30 days (24h '
    'interval), Tier B = clicked within 90 days (3-day interval). NULL = minted, never clicked = '
    'no signal, which resolves toward Tier C (more suppression, never less).';
