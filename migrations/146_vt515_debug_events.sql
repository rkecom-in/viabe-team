-- 146_vt515_debug_events.sql — VT-515: first-class debug/failure log.
--
-- Creates the ``debug_events`` table — a structured, PII-redacted record of
-- every signup→discovery→verify→create→OTP failure (including silent-degrades
-- that previously fell through unlogged).
--
-- Consumer: the team-web debug viewer subscribes via Supabase Realtime (INSERT
-- events) so the viewer renders failures within ~2s of occurrence. The viewer
-- is read-only; no client writes touch this table.
--
-- Access model: service-role only (mirrors migration 009 env_config).
-- ENABLE + FORCE RLS, explicit deny-all policy so no tenant-scoped role,
-- app_role, or anon key can read or write rows. The Realtime viewer uses
-- the service-role key (server-side only).
--
-- Realtime: REPLICA IDENTITY FULL + `supabase_realtime` publication, exactly
-- mirroring migration 030 (pipeline_steps / pipeline_runs). The DO $$ block
-- is idempotent in both vanilla Postgres (CI) and Supabase Prod.

CREATE TABLE IF NOT EXISTS public.debug_events (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ NOT NULL    DEFAULT now(),
    tenant_id     UUID        NULL,        -- nullable: pre-tenant failures have no tenant
    trace_id      TEXT        NULL,        -- correlation key (discovery_id / run_id / ...)
    failure_type  TEXT        NOT NULL,    -- exception|timeout|vendor_error|network|validation|crash|silent_degrade
    component     TEXT        NOT NULL,    -- signup|discovery|verify|create|otp|knowyourgst|sandbox|twilio|scrapingbee|anthropic|...
    operation     TEXT        NULL,        -- specific op (scrape|create_tenant|send_otp|invalid_gstin|...)
    error_message TEXT        NULL,        -- REDACTED (pii_redactor)
    error_stack   TEXT        NULL,        -- REDACTED, nullable
    context       JSONB       NULL,        -- REDACTED inputs/context
    severity      TEXT        NOT NULL,    -- warning|error|critical
    impact        TEXT        NULL,        -- blocked_signup|degraded_to_manual|degraded_to_X|failed_safe
    vendor        TEXT        NULL,        -- external vendor name if applicable
    vendor_status TEXT        NULL,        -- vendor HTTP/error code
    latency_ms    INT         NULL
);

-- Service-role only: deny-all policy (mirrors migration 009 env_config pattern).
-- RLS enabled + forced; explicit USING(false)/WITH CHECK(false) so NO role other
-- than the Supabase service-role key (which bypasses RLS structurally) can access
-- this table.
ALTER TABLE public.debug_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.debug_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS debug_events_no_tenant_access ON public.debug_events;
CREATE POLICY debug_events_no_tenant_access ON public.debug_events
    FOR ALL
    USING (false)
    WITH CHECK (false);

-- Indexes: tenant+time, trace (cross-source correlation), and created_at (time-range scans).
CREATE INDEX IF NOT EXISTS debug_events_tenant_created_idx
    ON public.debug_events (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS debug_events_trace_id_idx
    ON public.debug_events (trace_id)
    WHERE trace_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS debug_events_created_at_idx
    ON public.debug_events (created_at DESC);

-- REPLICA IDENTITY FULL: Supabase Realtime broadcasts the full row payload
-- (not just PKs) so the viewer receives the complete event without a re-fetch.
-- Mirrors migration 030 (pipeline_steps / pipeline_runs).
ALTER TABLE public.debug_events REPLICA IDENTITY FULL;

-- Add to Supabase Realtime publication.
-- Mirrors the idempotent DO $$ block in migration 030: short-circuits in vanilla
-- Postgres / CI environments where supabase_realtime doesn't exist, so the
-- migration stays idempotent in both CI and Supabase Prod.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime'
    ) THEN
        RAISE NOTICE 'supabase_realtime publication absent — skipping ADD TABLE (vanilla Postgres / CI env)';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_publication_tables
        WHERE pubname  = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename  = 'debug_events'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.debug_events;
    END IF;
END
$$;
