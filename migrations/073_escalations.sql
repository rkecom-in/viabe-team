-- 073_escalations.sql — VT-292 (Ops Console V2) escalation queue.
--
-- The canonical escalations the VTR works (the VT-290 Home seam currently derives from
-- pipeline_runs markers — VT-292 repoints to THIS table). The orchestrator writes a row
-- when it escalates (explicit, richer than a pipeline_runs status); a v1 backfill seeds
-- from pipeline_runs so Home isn't empty. Cowork-approved 2026-06-02 (answer #1).
--
-- Enforcement (answer #2): deny-all FORCE RLS — service-role only; VTR scoping is app-side
-- in team-web (assignment subquery, fail-closed), consistent with the VT-290 / VT-188
-- operator substrate (the operator path has no tenant GUC). Per-field columns (CL-417),
-- no JSONB. CL-422 synthetic. Migration 073 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.escalations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    run_id       UUID NULL,                      -- the pipeline_run that escalated, if any
    kind         TEXT NOT NULL,                  -- e.g. hard_limit | agent_escalated | error
    severity     TEXT NOT NULL DEFAULT 'medium', -- low | medium | high
    status       TEXT NOT NULL DEFAULT 'open',
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ NULL,
    resolved_by  UUID NULL,                       -- operator_id who resolved
    notes        TEXT NULL,
    CONSTRAINT escalations_status_chk CHECK (status IN ('open', 'ack', 'resolved')),
    CONSTRAINT escalations_severity_chk CHECK (severity IN ('low', 'medium', 'high'))
);

-- Open-queue scan (the VTR's hot path) + per-tenant scoping.
CREATE INDEX IF NOT EXISTS idx_escalations_open
    ON public.escalations (opened_at DESC) WHERE status <> 'resolved';
CREATE INDEX IF NOT EXISTS idx_escalations_tenant
    ON public.escalations (tenant_id);
-- v1 backfill idempotency: one escalation per run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_escalations_run
    ON public.escalations (run_id) WHERE run_id IS NOT NULL;

ALTER TABLE public.escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.escalations FORCE ROW LEVEL SECURITY;
