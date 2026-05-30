-- 052_pending_approvals.sql — VT-47 Pillar-7 owner-approval gate substrate.
--
-- request_owner_approval is the AUTHORITATIVE pause gate for sensitive
-- actions (campaign sends, cohort-size-exceeded, sensitive-data access).
-- The orchestrator owns the pause/resume state machine (Pillar 1); the
-- LangGraph node emits the pause via langgraph.types.interrupt() and this
-- table is the durable record of "who must approve what, and what they
-- decided". The graph checkpoint (checkpoints/* tables, RLS'd in graph.py)
-- holds the suspended execution; this row holds the human-decision state.
--
-- Pillar 7: a campaign send proceeds ONLY when a row reaches
-- decision='approved'. An unresolved row (decision NULL) or a non-approved
-- decision (rejected / needs_changes / timeout) does NOT proceed to send.
-- The agent cannot bypass this — the send is downstream of resume.
--
-- Pillar 3: RLS lives in the SAME migration that creates the table
-- (mirror 045_customers / 005_pipeline_runs). Tenant isolation via the
-- app_current_tenant() GUC helper (migration 000b; CL-82/88). ENABLE +
-- FORCE + 4 policies.
--
-- CL-422: dev holds SYNTHETIC data only until prod-in-Mumbai (VT-231).
-- CL-390: no PII columns. owner_message_sid is a Twilio SID, not content.
--
-- Migration number: 052 EXACTLY (Cowork CL-424 assignment; main's
-- .next-migration = 052). The runner applies by filename + tracks by name
-- (schema_migrations.name), so 052 merging in any order relative to
-- in-flight siblings does NOT skip them — order-independent here.
--
-- This migration ALSO extends pipeline_runs.status to accept the new
-- terminal value 'paused' (Cowork RULING, BINDING): a run that pauses on
-- an owner-approval interrupt is a distinct terminal value, not 'running'
-- and not 'completed'. See the ALTER at the bottom.

-- ======================= pending_approvals ===========================

CREATE TABLE IF NOT EXISTS public.pending_approvals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- thread_id == run_id; the suspended LangGraph checkpoint resumes on
    -- this run_id. FK to pipeline_runs keeps the approval bound to a real
    -- run (and lets RLS on the checkpoint tables share the same key).
    run_id            UUID NOT NULL REFERENCES pipeline_runs (id) ON DELETE CASCADE,
    -- campaign_id is NULL for non-campaign approvals (sensitive_data_access,
    -- cohort_size_exceeded before a campaign row exists).
    campaign_id       UUID NULL,
    approval_type     TEXT NOT NULL
                      CHECK (approval_type IN (
                          'campaign_send', 'cohort_size_exceeded',
                          'sensitive_data_access', 'other')),
    summary           TEXT NOT NULL CHECK (char_length(summary) <= 500),
    details           JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected', 'timed_out')),
    -- decision is NULL while pending; set when resolved. Distinct from
    -- status so the resume path can record the raw owner decision verb
    -- (approved/rejected/needs_changes/timeout) even when status collapses
    -- needs_changes -> rejected at the gate. Pillar-7: an absent decision
    -- never proceeds to send.
    decision          TEXT NULL
                      CHECK (decision IS NULL OR decision IN (
                          'approved', 'rejected', 'needs_changes', 'timeout')),
    owner_message_sid TEXT NULL,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    timeout_at        TIMESTAMPTZ NOT NULL,
    resolved_at       TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sweep scan: open approvals past timeout. Partial index on the unresolved
-- rows keyed by timeout_at — the 5th scheduled trigger scans
-- (resolved_at IS NULL AND timeout_at <= now()).
CREATE INDEX IF NOT EXISTS idx_pending_approvals_sweep
    ON public.pending_approvals (timeout_at)
    WHERE resolved_at IS NULL;

-- Resume lookup: the most-recent OPEN approval for a tenant/run.
CREATE INDEX IF NOT EXISTS idx_pending_approvals_open
    ON public.pending_approvals (tenant_id, run_id, requested_at)
    WHERE resolved_at IS NULL;

ALTER TABLE public.pending_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_approvals FORCE ROW LEVEL SECURITY;

CREATE POLICY pending_approvals_select ON public.pending_approvals
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY pending_approvals_insert ON public.pending_approvals
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_approvals_update ON public.pending_approvals
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY pending_approvals_delete ON public.pending_approvals
    FOR DELETE USING (tenant_id = app_current_tenant());

-- ============== pipeline_runs.status += 'paused' (Cowork RULING) ============
--
-- The CHECK in 005_pipeline_runs.sql is an inline (unnamed) constraint:
--   status IN ('running','completed','escalated','aborted_hard_limit',
--              'duplicate_rejected')
-- Postgres auto-names an inline column CHECK ``<table>_<column>_check``, i.e.
-- ``pipeline_runs_status_check``. DROP it by that name and re-ADD with the
-- new 'paused' terminal so a run that pauses on an owner-approval interrupt
-- is a distinct, queryable terminal value (NOT 'running', NOT 'completed').
-- Resume/timeout drives it onward (paused -> completed) via close_webhook_run.
ALTER TABLE public.pipeline_runs
    DROP CONSTRAINT IF EXISTS pipeline_runs_status_check;
ALTER TABLE public.pipeline_runs
    ADD CONSTRAINT pipeline_runs_status_check CHECK (status IN (
        'running', 'completed', 'escalated',
        'aborted_hard_limit', 'duplicate_rejected', 'paused'));
