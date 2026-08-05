-- VT-733 slice A — the cost console's read RPCs, plus the DB-side half of the VTR/VTAdmin split.
--
-- Fazal 2026-08-05: "The VTR and the VTRAdmin (me) should be able to have control over the prices
-- being spent behind a particular tenant… we can know how much is being consumed by the Manager,
-- the specialists and any other integrations we have."
--
-- Aggregation lives here rather than in the web layer for the reason the ops console already uses
-- RPCs (mig 133): a per-tenant × per-agent × per-model rollup over the whole ledger is a GROUP BY,
-- and shipping raw rows to Node to sum them would move both the cost and the PII surface.
--
-- Verified before writing (deployed dev, 2026-08-05):
--   * `global_llm_limits` holds ONE row with enabled=true, soft_pct=80 and BOTH cost ceilings NULL;
--     `tenant_llm_limits` is EMPTY. The enforcement half (mig 173) is switched on and configured to
--     no limit, so `budget_gate` cannot return a hard verdict today. The console must therefore show
--     "no cap set" as a first-class state, not render a comfortable 0%.
--   * The caps tables are `rls_on + rls_forced` with SELECT-only policies for {public} and NO write
--     policy at all — so writes are already denied to every non-BYPASSRLS role. What is NOT true is
--     the docstring claim that "only VTR admin can set limits": nothing distinguishes VTR from
--     VTAdmin in the database. That distinction lives in the WEB layer (requireOpsOperator +
--     assignedTenants), which is where slice A wires cap writes.

-- ---------------------------------------------------------------------------
-- 1. Close the loaded-gun grants.
-- ---------------------------------------------------------------------------
-- `anon` and `authenticated` hold table-level INSERT/UPDATE/DELETE on the caps tables. They are
-- inert TODAY only because no write policy exists — the day someone adds a permissive one, those
-- grants go live and a browser-side token could raise its own tenant's ceiling. Revoke them now,
-- while it costs nothing. Existence-guarded so local/CI Postgres (no Supabase roles) applies clean.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON tenant_llm_limits FROM anon;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON global_llm_limits FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON tenant_llm_limits FROM authenticated;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON global_llm_limits FROM authenticated;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Per-tenant spend, broken out by the axes Fazal named.
-- ---------------------------------------------------------------------------
-- p_tenant_ids NULL = every tenant (VTAdmin/Fazal); a non-null array = the caller's assigned set
-- (VTR). The narrowing is passed IN, never derived from a client value — the route computes it from
-- the operator's own assignment (the Ops-mutation IDOR pattern, caught twice before).
CREATE OR REPLACE FUNCTION ops_tenant_cost_summary(
    p_since timestamptz,
    p_tenant_ids uuid[] DEFAULT NULL
)
RETURNS TABLE (
    tenant_id uuid,
    business_name text,
    agent text,
    model text,
    calls bigint,
    tokens_in bigint,
    tokens_out bigint,
    cost_usd numeric,
    search_cost_usd numeric
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    SELECT e.tenant_id,
           t.business_name,
           e.agent,
           e.model,
           count(*)::bigint                              AS calls,
           coalesce(sum(e.tokens_in), 0)::bigint         AS tokens_in,
           coalesce(sum(e.tokens_out), 0)::bigint        AS tokens_out,
           coalesce(sum(e.cost_usd), 0)::numeric         AS cost_usd,
           coalesce(sum(e.search_cost_usd), 0)::numeric  AS search_cost_usd
    FROM llm_call_events e
    JOIN tenants t ON t.id = e.tenant_id
    WHERE e.occurred_at >= p_since
      AND (p_tenant_ids IS NULL OR e.tenant_id = ANY(p_tenant_ids))
    GROUP BY e.tenant_id, t.business_name, e.agent, e.model
    ORDER BY sum(e.cost_usd) DESC NULLS LAST
$$;

-- ---------------------------------------------------------------------------
-- 3. Cap status per tenant — including the "no cap set" state, stated honestly.
-- ---------------------------------------------------------------------------
-- Spend is measured over the SAME window the cap governs (day/month), so distance-to-cap is a real
-- number rather than a mixed-window comparison. A NULL ceiling yields NULL distance and
-- cap_state='none' — the console renders that as "no ceiling", never as "0% used", because the
-- second reads as safety and today there IS none.
CREATE OR REPLACE FUNCTION ops_tenant_cap_status(p_tenant_ids uuid[] DEFAULT NULL)
RETURNS TABLE (
    tenant_id uuid,
    business_name text,
    spend_today_usd numeric,
    spend_month_usd numeric,
    max_cost_usd_day numeric,
    max_cost_usd_month numeric,
    soft_pct integer,
    caps_enabled boolean,
    cap_state text
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    WITH g AS (SELECT * FROM global_llm_limits LIMIT 1),
    spend AS (
        SELECT e.tenant_id,
               coalesce(sum(e.cost_usd) FILTER (
                   WHERE e.occurred_at >= date_trunc('day', now())), 0)::numeric   AS today_usd,
               coalesce(sum(e.cost_usd) FILTER (
                   WHERE e.occurred_at >= date_trunc('month', now())), 0)::numeric AS month_usd
        FROM llm_call_events e
        WHERE p_tenant_ids IS NULL OR e.tenant_id = ANY(p_tenant_ids)
        GROUP BY e.tenant_id
    )
    SELECT t.id AS tenant_id,
           t.business_name,
           coalesce(s.today_usd, 0)::numeric  AS spend_today_usd,
           coalesce(s.month_usd, 0)::numeric  AS spend_month_usd,
           coalesce(l.max_cost_usd_day,   (SELECT max_cost_usd_day   FROM g)) AS max_cost_usd_day,
           coalesce(l.max_cost_usd_month, (SELECT max_cost_usd_month FROM g)) AS max_cost_usd_month,
           coalesce(l.soft_pct, (SELECT soft_pct FROM g))            AS soft_pct,
           coalesce((SELECT enabled FROM g), false)                  AS caps_enabled,
           CASE
               WHEN coalesce(l.max_cost_usd_day,   (SELECT max_cost_usd_day   FROM g)) IS NULL
                AND coalesce(l.max_cost_usd_month, (SELECT max_cost_usd_month FROM g)) IS NULL
                   THEN 'none'
               WHEN coalesce(s.today_usd, 0) >= coalesce(l.max_cost_usd_day,
                        (SELECT max_cost_usd_day FROM g))
                 OR coalesce(s.month_usd, 0) >= coalesce(l.max_cost_usd_month,
                        (SELECT max_cost_usd_month FROM g))
                   THEN 'hard'
               WHEN coalesce(s.today_usd, 0) >= coalesce(l.max_cost_usd_day,
                        (SELECT max_cost_usd_day FROM g)) * coalesce(l.soft_pct,
                        (SELECT soft_pct FROM g), 80) / 100.0
                   THEN 'soft'
               ELSE 'ok'
           END AS cap_state
    FROM tenants t
    LEFT JOIN spend s ON s.tenant_id = t.id
    LEFT JOIN tenant_llm_limits l ON l.tenant_id = t.id
    WHERE p_tenant_ids IS NULL OR t.id = ANY(p_tenant_ids)
    ORDER BY coalesce(s.month_usd, 0) DESC
$$;

-- ---------------------------------------------------------------------------
-- 4. Exposure: service_role only, never PostgREST-public.
-- ---------------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION ops_tenant_cost_summary(timestamptz, uuid[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ops_tenant_cap_status(uuid[]) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION ops_tenant_cost_summary(timestamptz, uuid[]) TO service_role;
        GRANT EXECUTE ON FUNCTION ops_tenant_cap_status(uuid[]) TO service_role;
    END IF;
END $$;

COMMENT ON FUNCTION ops_tenant_cost_summary(timestamptz, uuid[]) IS
    'VT-733A: per-tenant LLM spend by agent + model since a cutoff. p_tenant_ids NULL = all '
    '(VTAdmin/Fazal); an array = the VTR''s assigned set, computed server-side from the operator.';
COMMENT ON FUNCTION ops_tenant_cap_status(uuid[]) IS
    'VT-733A: per-tenant spend vs cap over the cap''s OWN window, with cap_state none|ok|soft|hard. '
    '"none" is first-class: as of 2026-08-05 no ceilings are configured at all.';
