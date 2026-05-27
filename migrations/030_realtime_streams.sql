-- 030_realtime_streams.sql — VT-201 ops live stream Realtime substrate.
--
-- Enables Supabase Realtime broadcasts for INSERTs on pipeline_steps +
-- pipeline_runs so the Ops Console /ops/stream page can subscribe and
-- render events live (<2s lag target per VT-201 AC-1).
--
-- Per VT-201 Q2 (Option B locked, Cowork plan-review 2026-05-27 Phase-1
-- only): direct browser → Supabase Realtime subscription with the
-- operator-claim JWT from VT-188 substrate. Phase 2 (multi-operator)
-- migrates to a server-side SSE proxy; see `lib/ops/stream.ts` docstring
-- for migration path.
--
-- Mechanism:
--   1. REPLICA IDENTITY FULL on both tables so realtime broadcasts carry
--      full row payload (not just primary keys)
--   2. ADD TABLE to the `supabase_realtime` publication so the WAL
--      changes flow to Realtime
--   3. NEW RLS policy on each table: SELECT permitted when the JWT
--      carries `operator_claim=true` (mirrors VT-188 phone-resolution
--      pattern — Phase-1 single operator = Fazal)
--
-- Per CL-71: existing tenant-scoped RLS policies remain in force. The
-- operator-claim policy is ADDITIVE (permissive) — joining the existing
-- restrictive policies via PostgreSQL's policy-OR semantics. Operator
-- sees all tenants; regular tenant connections see only their own.
--
-- Per CL-416 + CL-390: stream observability is read-only; no delete
-- paths added.
--
-- PHASE 2 NOTE (Cowork: document migration path):
-- When multiple operators come online, this Q2-Option-B (direct browser
-- with operator JWT) should migrate to Q2-Option-A (server-side SSE
-- proxy via Next.js API route). The Phase-2 migration:
--   1. Remove the operator-claim SELECT policies below
--   2. Stream events server-side (Next.js API route subscribes via
--      service-role; forwards to per-operator SSE connections)
--   3. Per-operator filtering moves to server-side state
-- This keeps cache coherency + scopes JWT exposure to a server context.

-- 1. REPLICA IDENTITY FULL — Realtime needs full row data for the JSON payload
ALTER TABLE pipeline_steps REPLICA IDENTITY FULL;
ALTER TABLE pipeline_runs REPLICA IDENTITY FULL;

-- 2. Add to Realtime publication. `supabase_realtime` is the default
-- publication created by the Supabase Realtime extension. CI runs
-- against vanilla Postgres without that extension; the DO block
-- short-circuits when the publication doesn't exist so the migration
-- stays idempotent in both environments. Production Supabase deploys
-- have the publication; the table will be added there.
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
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'pipeline_steps'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.pipeline_steps;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'pipeline_runs'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.pipeline_runs;
    END IF;
END
$$;

-- 3. Operator-claim SELECT policy — additive PERMISSIVE policy. JWT
-- claim `operator_claim=true` set by team-web's `issueOperatorJwt`
-- (VT-188 + VT-123 lib/auth/operator-jwt.ts).
DROP POLICY IF EXISTS pipeline_steps_operator_select ON public.pipeline_steps;
CREATE POLICY pipeline_steps_operator_select ON public.pipeline_steps
    AS PERMISSIVE
    FOR SELECT
    TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

DROP POLICY IF EXISTS pipeline_runs_operator_select ON public.pipeline_runs;
CREATE POLICY pipeline_runs_operator_select ON public.pipeline_runs
    AS PERMISSIVE
    FOR SELECT
    TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON POLICY pipeline_steps_operator_select ON public.pipeline_steps IS
    'VT-201 Phase-1: operator JWT (Fazal) sees all tenants for /ops/stream. Phase-2 migrate to server-side SSE proxy.';
COMMENT ON POLICY pipeline_runs_operator_select ON public.pipeline_runs IS
    'VT-201 Phase-1: operator JWT (Fazal) sees all tenants for /ops/stream. Phase-2 migrate to server-side SSE proxy.';
