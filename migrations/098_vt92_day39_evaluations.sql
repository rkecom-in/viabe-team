-- VT-92 — day39_evaluations: persisted decision-audit for the day-39 evaluator.
--
-- The evaluator (billing/day39_evaluator.py) already computes the verdict + emits a
-- pipeline_log event. This table persists the STRUCTURED decision so future re-runs
-- can verify against history (reproducibility — VT-92 D4) and so the 90-day
-- post-CONTINUE suppression has a durable substrate.
--
-- Tenant-scoped: RLS ENABLE + FORCE + app_current_tenant() policies. Written by the
-- cross-tenant scheduled sweep (service_role, allowlisted). DSR: it carries the
-- tenant's ARRR/fees (subject billing data) → in dsr_purge._PURGE_ORDER (hard-delete
-- on a tenant DSR; the FK CASCADE never fires since DSR anonymizes, not deletes).

CREATE TABLE IF NOT EXISTS public.day39_evaluations (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    verdict               TEXT NOT NULL
                          CHECK (verdict IN ('continue', 'refund_triggered', 'not_eligible')),
    arrr_paise            BIGINT NOT NULL,
    cumulative_fees_paise BIGINT NOT NULL,
    evaluator_version     TEXT NOT NULL,
    evaluated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_day39_evaluations_tenant
    ON public.day39_evaluations (tenant_id, evaluated_at DESC);

ALTER TABLE public.day39_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.day39_evaluations FORCE ROW LEVEL SECURITY;

CREATE POLICY day39_evaluations_select ON public.day39_evaluations
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY day39_evaluations_insert ON public.day39_evaluations
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY day39_evaluations_update ON public.day39_evaluations
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY day39_evaluations_delete ON public.day39_evaluations
    FOR DELETE USING (tenant_id = app_current_tenant());
