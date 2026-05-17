-- 000b_rls_helpers.sql — shared helpers for Row-Level Security policies.
--
-- Pillar 3: tenant isolation is structural. Every tenant-scoped table enables
-- RLS in its own migration and keys its policies off the request's tenant.
--
-- The tenant context is carried as a session GUC, `app.current_tenant`, set by
-- the application's typed wrappers (VT-8). When the GUC is unset or empty this
-- returns NULL, so an un-scoped connection matches no tenant rows.
--
-- The Supabase secret key / Postgres superuser bypasses RLS entirely — that is
-- the intended service-role path. RLS is FORCED on every table so that table
-- ownership alone does not bypass it.
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
    LANGUAGE sql
    STABLE
AS $$
    SELECT NULLIF(current_setting('app.current_tenant', true), '')::uuid
$$;
