-- 133_vt376_ops_top_tenants_today_fn.sql — VT-376: ship the ops tenant-listing RPC the
-- Ops Console has called since the fleet pages landed but which existed in NO migration
-- (a manual artifact at best — absent on dev Seoul, where `client.rpc(...)` errored and
-- `?? []` silently rendered every tenant list empty; surfaced by the VT-376 binding e2e
-- precondition).
--
-- Shape derived from the sole caller, apps/team-web/lib/ops/data-access.ts::fetchTopTenants
-- (p_limit, p_since → TopTenantRow{tenant_id, business_name, runs_count}). Ranking:
-- pipeline_runs activity since p_since; LEFT JOIN so zero-run tenants still list (the
-- panel must show pausable tenants even on quiet days).
--
-- Caller is the server-side SECRET client only (service_role). Deny-by-default: EXECUTE
-- revoked from anon/authenticated; SECURITY INVOKER (service_role already holds the
-- table rights; no privilege escalation surface).

CREATE OR REPLACE FUNCTION ops_top_tenants_today(p_limit integer, p_since timestamptz)
RETURNS TABLE (tenant_id uuid, business_name text, runs_count bigint)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    SELECT t.id AS tenant_id,
           t.business_name,
           count(r.id)::bigint AS runs_count
    FROM tenants t
    LEFT JOIN pipeline_runs r
           ON r.tenant_id = t.id AND r.started_at >= p_since
    GROUP BY t.id, t.business_name
    ORDER BY count(r.id) DESC, t.created_at DESC
    LIMIT p_limit
$$;

-- PUBLIC revoke covers anon/authenticated (PostgREST exposure rides PUBLIC's default
-- EXECUTE); the explicit service_role grant is existence-guarded so local/CI Postgres
-- (no Supabase roles) applies clean — the mig-130 guard pattern.
REVOKE EXECUTE ON FUNCTION ops_top_tenants_today(integer, timestamptz) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION ops_top_tenants_today(integer, timestamptz) TO service_role;
    END IF;
END $$;
