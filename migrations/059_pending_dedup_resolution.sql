-- 059_pending_dedup_resolution.sql — VT-54 / VT-6.3 ambiguous-merge parking.
--
-- When the dedup primitive cannot AUTO-resolve a candidate (e.g. an incoming row
-- matches >1 existing customer, or a name-only fuzzy match with no phone/email),
-- it parks the candidate here and (VT-53) asks the owner. P4: an ambiguous merge
-- is NEVER auto-committed with a guess — it waits for the owner.
--
-- Pillar 3: RLS in the same migration (CL-82/88). CL-422: dev = synthetic only.
-- Migration number 059 via scripts/migration_id_allocate.py (the VT-54 row's
-- legacy "migration 016" text is stale — superseded per the plan review, D3).

CREATE TABLE IF NOT EXISTS public.pending_dedup_resolution (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- Existing customers the incoming row plausibly matches (>=1).
    candidate_customer_ids UUID[] NOT NULL,
    -- The incoming canonical row that couldn't be auto-merged (no PII beyond
    -- what the owner already holds; CL-422 synthetic on dev).
    incoming     JSONB NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'resolved', 'dropped')),
    clarification_id UUID NULL,  -- links to pending_clarifications when asked (VT-53)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_dedup_tenant_status
    ON public.pending_dedup_resolution (tenant_id, status);

ALTER TABLE public.pending_dedup_resolution ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_dedup_resolution FORCE ROW LEVEL SECURITY;

CREATE POLICY pending_dedup_resolution_select ON public.pending_dedup_resolution
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY pending_dedup_resolution_insert ON public.pending_dedup_resolution
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_dedup_resolution_update ON public.pending_dedup_resolution
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_dedup_resolution_delete ON public.pending_dedup_resolution
    FOR DELETE USING (tenant_id = app_current_tenant());
