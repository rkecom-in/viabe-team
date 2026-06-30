-- 147_vt514_tm_audit_log.sql — VT-514: Team-Manager AUDIT / TRACE log.
--
-- The unified audit SPINE over everything the Team-Manager (and its specialist
-- lanes) KNOWS / GETS / DECIDES / DOES / ASKS. This is NOT a parallel
-- observability system: reasoning DEPTH stays in pipeline_steps
-- (agent_reasoning_step, already realtime via mig 030) and failures stay in
-- debug_events (mig 146). tm_audit_log REFERENCES both —
--   * reasoning_ref → {run_id, step_seq|step_id} into pipeline_steps (no think_text dup)
--   * trace_id      → joins the correlated debug_events failure row
-- — and adds the two columns the existing substrate structurally lacks:
-- trace_id (first-class) + snapshot_id (point-in-time knowledge replay).
--
-- Completeness-by-construction: rows are emitted at the single choke-points of
-- the action / decision / event layers (see observability/tm_audit.py). The
-- ACTION layer emits INSIDE the caller's transaction (fail-closed) so a
-- DB-transactional side-effect cannot commit without its audit row — the
-- VT-460 rails non-bypassability analog (proven by
-- tests/agent/test_tm_audit_nonbypassability.py).
--
-- PII: every free-text/JSONB column is PII-redacted by the emit helper
-- (pii_redactor + tenant name_registry, CL-390) BEFORE insert. No raw PII at
-- rest; the VTR viewer receives only ids + structured facts + tokens.
--
-- Retention: DSR-purge-scoped (tenant_id NOT NULL, purged with the tenant via
-- the VT-185 DSR path) — execution telemetry, NOT 7-year hash-chained forensics
-- like privacy_audit_log.
--
-- Access model: app_role may INSERT only its own tenant's rows (so the
-- in-transaction action emit commits atomically under RLS). Reads are
-- operator-JWT only (operator_claim=true), mirroring mig 030 pipeline_steps —
-- NOT debug_events' deny-all (that left VT-515's browser realtime dead; this
-- migration back-fills the same operator policy onto debug_events too).
--
-- Realtime: REPLICA IDENTITY FULL + supabase_realtime publication, idempotent
-- DO $$ block mirroring mig 030 / 146 (short-circuits in vanilla Postgres/CI).

CREATE TABLE IF NOT EXISTS public.tm_audit_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL    DEFAULT now(),
    tenant_id       UUID        NOT NULL    REFERENCES tenants(id),
    run_id          UUID        NULL        REFERENCES pipeline_runs(id),
    trace_id        TEXT        NULL,        -- cross-source key (= str(run_id)/discovery_id) → joins debug_events
    snapshot_id     TEXT        NULL,        -- sha256 of point-in-time knowledge; references the KNOWS row that stored the blocks
    event_layer     TEXT        NOT NULL,    -- knows|gets|decides|does|asks
    event_kind      TEXT        NOT NULL,    -- context_assembled|inbound_received|retrieval|intent_classified|route_decided|model_tier_selected|reasoning_turn|self_eval|policy_applied|spawn|draft_created|send_armed|send_result|approval_armed|approval_resolved|escalation|memory_write|autonomy_change|business_action|ask_owner
    actor           TEXT        NOT NULL,    -- team_manager|sales_recovery|integration|onboarding_conductor|<lane>
    summary         TEXT        NULL,        -- REDACTED single-line feed label
    input           JSONB       NULL,        -- REDACTED inbound + retrieved, expected-vs-got
    decision        JSONB       NULL,        -- REDACTED intent/route/outcome/self-eval/policy + reasoning
    reasoning_ref   JSONB       NULL,        -- {run_id, step_seq|step_id} pointer into pipeline_steps (no think_text dup)
    action          JSONB       NULL,        -- REDACTED what was done + params
    result          JSONB       NULL,        -- REDACTED outcome / ids
    severity        TEXT        NOT NULL    DEFAULT 'info',  -- info|warning|error|critical
    status          TEXT        NOT NULL    DEFAULT 'ok',    -- ok|failed|blocked|pending
    parent_audit_id UUID        NULL        REFERENCES tm_audit_log(id)
);

-- RLS: app_role INSERT (own tenant only) + operator-JWT SELECT. No app_role
-- SELECT — TM audit is operator/VTR-facing, not tenant-facing; the emit helper
-- generates the UUID client-side so it never needs RETURNING-under-RLS.
ALTER TABLE public.tm_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tm_audit_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tm_audit_app_insert ON public.tm_audit_log;
CREATE POLICY tm_audit_app_insert ON public.tm_audit_log
    AS PERMISSIVE
    FOR INSERT
    TO app_role
    WITH CHECK (tenant_id = app_current_tenant());

-- Operator-claim SELECT — VERBATIM mirror of mig 030 pipeline_steps_operator_select
-- so the VT-516 operator JWT actually RECEIVES realtime (the service-role key is
-- server-side only and must never reach the browser).
DROP POLICY IF EXISTS tm_audit_operator_select ON public.tm_audit_log;
CREATE POLICY tm_audit_operator_select ON public.tm_audit_log
    AS PERMISSIVE
    FOR SELECT
    TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON POLICY tm_audit_operator_select ON public.tm_audit_log IS
    'VT-514: operator JWT (Fazal) sees all tenants for the VTR TM-activity stream. Phase-2 migrate to server-side SSE proxy.';

-- Indexes: tenant+time (feed scan), run (per-run grouping), trace (cross-source
-- join to debug_events), tenant+layer+time (layer filter).
CREATE INDEX IF NOT EXISTS tm_audit_tenant_created_idx
    ON public.tm_audit_log (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tm_audit_run_idx
    ON public.tm_audit_log (run_id)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tm_audit_trace_idx
    ON public.tm_audit_log (trace_id)
    WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tm_audit_layer_idx
    ON public.tm_audit_log (tenant_id, event_layer, created_at DESC);

-- REPLICA IDENTITY FULL + Realtime publication (mirrors mig 030 / 146).
ALTER TABLE public.tm_audit_log REPLICA IDENTITY FULL;

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
        WHERE pubname    = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename  = 'tm_audit_log'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.tm_audit_log;
    END IF;
END
$$;

-- ── WITHIN-SCOPE FIX (VT-515 realtime gap) ─────────────────────────────────
-- debug_events (mig 146) is FORCE RLS deny-all (USING(false)) with NO operator
-- policy, so a browser operator JWT receives ZERO realtime rows — VT-515's live
-- "Debug / Failures" browser feed is currently dead (only the server-role
-- page-load prefetch shows). The VT-516 audit-vs-debug toggle needs the debug
-- stream live for operators, so back-fill the SAME additive operator-claim
-- SELECT policy. The deny-all FOR ALL policy still blocks app_role/anon/tenant
-- reads and ALL writes (PERMISSIVE policies are OR'd only within a command;
-- the operator policy is SELECT-only, so INSERT/UPDATE/DELETE stay deny-all).
DROP POLICY IF EXISTS debug_events_operator_select ON public.debug_events;
CREATE POLICY debug_events_operator_select ON public.debug_events
    AS PERMISSIVE
    FOR SELECT
    TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON POLICY debug_events_operator_select ON public.debug_events IS
    'VT-514/516: enable operator-JWT browser realtime for the Debug/Failures feed (VT-515 mig 146 left this deny-all → dead browser realtime). SELECT-only; writes stay service-role.';
