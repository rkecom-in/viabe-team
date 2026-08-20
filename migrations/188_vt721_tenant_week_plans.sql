-- 188_vt721_tenant_week_plans.sql — VT-721: the rolling 7-day plan, revised daily.
--
-- WHAT: a durable per-tenant 7-day plan object — the missing MIDDLE horizon between the §7A
-- monthly roadmap and the daily initiative pick. Each daily revision is a NEW row chained via
-- prev_plan_id (append-only, mirroring manager_asserted_facts): actions carry the §0.1d
-- directive+inputs+objective the Manager hands a specialist; revision_notes carry the WHY of
-- every keep/drop/resequence/add (CL-2026-07-29-manager-is-coo (c)).
--
-- PLAN IS NOT EFFECT (ARCHITECTURE §0.1.1): a row here schedules nothing by itself — execution
-- still passes the deterministic gates + Pillar-7. The revision writer enforces
-- requires_approval=true on every money/send action class at the application gate.
--
-- PRIVACY: tenant_id NOT NULL, ENABLE + FORCE RLS, CRUD policies via app_current_tenant().
-- Registered in dsr_purge._PURGE_ORDER in this SAME change set (leaf; FK tenants CASCADE never
-- fires — DSR anonymizes the tenants row; explicit sweep is the erasure path).
--
-- REVERSAL (not executed here): remove from _PURGE_ORDER, then DROP TABLE.

CREATE TABLE public.tenant_week_plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    plan_date       DATE NOT NULL,
    horizon_start   DATE NOT NULL,
    horizon_end     DATE NOT NULL,
    actions         JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(actions) = 'array'),
    revision_notes  JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(revision_notes) = 'array'),
    generated_by    TEXT NOT NULL DEFAULT 'manager'
                        CHECK (generated_by IN ('manager', 'seed', 'operator')),
    model_id        TEXT NULL,
    prev_plan_id    UUID NULL
                        REFERENCES public.tenant_week_plans (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_week_plans_horizon CHECK (horizon_end >= horizon_start),
    -- ONE revision per tenant per day — the daily pass is idempotent by construction.
    CONSTRAINT tenant_week_plans_daily_uniq UNIQUE (tenant_id, plan_date)
);

ALTER TABLE public.tenant_week_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_week_plans FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_week_plans_select ON public.tenant_week_plans FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_week_plans_insert ON public.tenant_week_plans FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_week_plans_update ON public.tenant_week_plans FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_week_plans_delete ON public.tenant_week_plans FOR DELETE
    USING (tenant_id = app_current_tenant());

-- The latest-plan read: newest revision for a tenant.
CREATE INDEX tenant_week_plans_latest
    ON public.tenant_week_plans (tenant_id, plan_date DESC);
