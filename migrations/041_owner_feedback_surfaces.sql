-- VT-198 — owner feedback substrate (3-tier: implicit / emoji / dashboard)
--
-- Append-only. RLS by tenant_id on SELECT + INSERT. NO PII in
-- source_metadata (CL-390 lock). Implicit tier has partial unique
-- index so daily scheduled re-runs don't double-write.

CREATE TABLE IF NOT EXISTS public.owner_feedback (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    run_id UUID,
    tier TEXT NOT NULL CHECK (tier IN ('implicit', 'emoji', 'dashboard')),
    signal TEXT NOT NULL,
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_owner_feedback_tenant_created
    ON public.owner_feedback (tenant_id, created_at DESC);

-- LOCK 2: partial unique on (tenant, run, tier='implicit') so a
-- repeated daily scheduler tick doesn't double-write implicit rows.
-- Emoji + dashboard tiers can have multiple rows per run.
CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_feedback_implicit_unique
    ON public.owner_feedback (tenant_id, run_id, tier)
    WHERE tier = 'implicit';

ALTER TABLE public.owner_feedback ENABLE ROW LEVEL SECURITY;

-- SELECT isolation
DROP POLICY IF EXISTS owner_feedback_tenant_isolation ON public.owner_feedback;
CREATE POLICY owner_feedback_tenant_isolation
    ON public.owner_feedback
    FOR SELECT
    USING (tenant_id = app_current_tenant());

-- LOCK 3: INSERT isolation. Without WITH CHECK, a misconfigured caller
-- could insert cross-tenant rows that schema-pass but SELECT-fail.
DROP POLICY IF EXISTS owner_feedback_tenant_insert ON public.owner_feedback;
CREATE POLICY owner_feedback_tenant_insert
    ON public.owner_feedback
    FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
