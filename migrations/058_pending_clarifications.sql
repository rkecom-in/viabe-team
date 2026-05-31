-- 058_pending_clarifications.sql — VT-53 / VT-6.2 clarifying-question flow (backend).
--
-- When vision extraction (VT-52) returns a field below the ask threshold (<0.7),
-- the ingestion path parks a bundled clarification here and (later, VT-9.4)
-- asks the owner. This migration is the BACKEND substrate only — owner-facing
-- WhatsApp templating is VT-9.4, out of scope for VT-53.
--
-- Pillar 3: RLS lives in the same migration that creates the table (CL-82/88).
-- Pillar 4: timeout => the row goes 'expired' and the original extraction is
--   DROPPED by the caller — never committed with a guessed value.
-- Pillar 8: ONE shared clarification table across all 9 ingestion methods.
-- CL-422: dev holds SYNTHETIC data only until prod-in-Mumbai (VT-231).
--
-- Migration number 058 via scripts/migration_id_allocate.py (the VT-53 row's
-- legacy "migration 015" text is stale — superseded per the VT-52/53/54 plan
-- review, D3). Runner applies by filename + tracks by schema_migrations.name,
-- so number order is not a dependency.

CREATE TABLE IF NOT EXISTS public.pending_clarifications (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- Opaque reference to the upload / extraction run this clarifies.
    subject_ref  TEXT NOT NULL,
    -- Bundled questions: [{"field": "...", "prompt": "..."}], 1..3 (VT-6 max-3).
    questions    JSONB NOT NULL
                 CHECK (jsonb_typeof(questions) = 'array'
                        AND jsonb_array_length(questions) BETWEEN 1 AND 3),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'answered', 'expired')),
    -- {field: resolved_value} once the owner answers; NULL while pending.
    resolution   JSONB NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- created_at + timeout (app-set; 7d default, Type-2 to change).
    expires_at   TIMESTAMPTZ NOT NULL,
    resolved_at  TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_clarifications_tenant_status
    ON public.pending_clarifications (tenant_id, status);
-- Sweep lookup: pending rows past their deadline.
CREATE INDEX IF NOT EXISTS idx_pending_clarifications_expiry
    ON public.pending_clarifications (expires_at)
    WHERE status = 'pending';

ALTER TABLE public.pending_clarifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_clarifications FORCE ROW LEVEL SECURITY;

CREATE POLICY pending_clarifications_select ON public.pending_clarifications
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY pending_clarifications_insert ON public.pending_clarifications
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_clarifications_update ON public.pending_clarifications
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_clarifications_delete ON public.pending_clarifications
    FOR DELETE USING (tenant_id = app_current_tenant());
