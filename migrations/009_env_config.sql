-- 009_env_config.sql — runtime configuration that does not belong in env vars.
-- Workspace-wide (not tenant-scoped).
CREATE TABLE env_config (
    key        TEXT PRIMARY KEY,
    value      JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

-- Service-role-only: RLS enabled + forced with a deny-all policy. Reachable
-- only via the RLS-bypassing Supabase secret key / Postgres superuser.
ALTER TABLE env_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE env_config FORCE ROW LEVEL SECURITY;

CREATE POLICY env_config_no_tenant_access ON env_config
    FOR ALL
    USING (false)
    WITH CHECK (false);
