-- 015_app_role.sql — non-superuser role for tenant-scoped DB writes (CL-71).
--
-- Per CL-122: the tenant_connection() wrapper does SET ROLE app_role so the 12
-- tenant-scoped writers run under a role where FORCE ROW LEVEL SECURITY is
-- actually enforced. DBOS, the LangGraph checkpointer, _lookup_tenant and
-- _within_rate_limits keep the privileged role (migration 000b's intended
-- service-role path).
--
-- app_role has NO BYPASSRLS, NO SUPERUSER, and only the DML needed for tenant
-- tables. It is NOLOGIN — entered only via SET ROLE from the privileged role
-- that authenticates the connection.

CREATE ROLE app_role NOLOGIN;

-- The public schema grants USAGE to PUBLIC by default, but be explicit.
GRANT USAGE ON SCHEMA public TO app_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    tenants,
    pipeline_runs,
    pipeline_steps,
    phase_transitions,
    twilio_inbound_events,
    dsr_tickets,
    rate_limit_buckets
TO app_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_role;

-- RLS policy expressions call app_current_tenant() as the querying role.
GRANT EXECUTE ON FUNCTION app_current_tenant() TO app_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_role;

-- Grant app_role membership to the role running this migration so SET ROLE
-- works at runtime. In CI that role is `postgres`; in Supabase/production it
-- is the secret-key role. This assumes the migration-runner role and the
-- runtime connection role are the same — verified true for both today.
DO $$
BEGIN
    EXECUTE format('GRANT app_role TO %I', current_user);
END $$;
