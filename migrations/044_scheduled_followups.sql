-- 044_scheduled_followups.sql — VT-48 schedule_followup substrate.
--
-- The schedule_followup MCP tool enqueues a future orchestrator
-- invocation (e.g. "follow up on this campaign in 3 days if no owner
-- response"). The orchestrator scheduler (VT-3.5, out of scope here)
-- polls this table; this migration + the tool only provide the
-- idempotent row-write primitive.
--
-- Pillar 1: the tool enqueues; the scheduler runs. No execution here.
-- Pillar 8: idempotency via UNIQUE(tenant_id, follow_up_key) — the
--   agent commits to a stable key; a second identical key is a no-op.
-- Pillar 3: RLS lives in the same migration that creates the table
--   (CL-82 GUC convention via app_current_tenant()).
--
-- Note: legacy VT-48 row text said "014_scheduled_followups.sql" — that
-- number is taken (014_schema_hardening). Allocated 044 (next free;
-- 043 = webhook_metrics was latest on main). Cowork-confirmed 2026-05-30.

CREATE TABLE IF NOT EXISTS public.scheduled_followups (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id_origin  UUID NULL,        -- the run that scheduled it (nullable)
    follow_up_type TEXT NOT NULL CHECK (follow_up_type IN (
                       'campaign_followup', 'attribution_check',
                       'reengagement_reminder', 'other')),
    follow_up_key  TEXT NOT NULL,    -- stable idempotency key (agent-chosen)
    fire_at        TIMESTAMPTZ NOT NULL,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    cancel_if      JSONB NULL,       -- list of deterministic cancel conditions
    scheduled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at       TIMESTAMPTZ NULL,
    cancelled_at   TIMESTAMPTZ NULL,
    cancel_reason  TEXT NULL,
    -- Pillar 8 idempotency: same logical follow-up → same row.
    CONSTRAINT scheduled_followups_tenant_key_uniq
        UNIQUE (tenant_id, follow_up_key)
);

-- Scheduler poll index: due, not-yet-fired, not-cancelled rows.
CREATE INDEX IF NOT EXISTS idx_scheduled_followups_due
    ON public.scheduled_followups (fire_at)
    WHERE fired_at IS NULL AND cancelled_at IS NULL;

ALTER TABLE public.scheduled_followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_followups FORCE ROW LEVEL SECURITY;

-- Four policies (SELECT / INSERT / UPDATE / DELETE) mirror the campaigns
-- (016) + attributions (023) template. app_current_tenant() reads the
-- app.current_tenant GUC set by tenant_connection().
CREATE POLICY scheduled_followups_select ON public.scheduled_followups
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY scheduled_followups_insert ON public.scheduled_followups
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY scheduled_followups_update ON public.scheduled_followups
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY scheduled_followups_delete ON public.scheduled_followups
    FOR DELETE USING (tenant_id = app_current_tenant());
