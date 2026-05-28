-- 033_tenant_oauth_tokens.sql — VT-207 OAuth substrate.
--
-- Per-tenant per-connector encrypted refresh_token storage. Reuses
-- VT-191's Fernet substrate via the shared ``encrypt_value`` /
-- ``decrypt_value`` helpers (extracted from phone_tokens.py — both
-- modules now use the same key + Fernet wrapper).
--
-- Phase-1 assumes ONE connector instance per (tenant, connector_id) pair
-- per Cowork Q4 flag — composite PK enforces that. Multi-instance
-- (e.g., owner connects 2 different Sheets under same tenant) requires
-- an instance_id column. File follow-up VT-row when that pattern lands.
--
-- Per VT-191 / CL-390: refresh_token is encrypted at rest with the
-- same TEAM_PHONE_ENCRYPTION_KEY (rename to TEAM_FERNET_KEY in a
-- follow-up if the semantics warrant separation).
-- Per CL-19: typed per-field columns; scopes as TEXT[] not JSONB.
-- Per CL-71: tenant-scoped RLS.
-- Per CL-416: lifetime retention; DSR-purge owns deletion.

CREATE TABLE IF NOT EXISTS public.tenant_oauth_tokens (
    tenant_id              UUID NOT NULL REFERENCES tenants(id),
    connector_id           TEXT NOT NULL,
    refresh_token_encrypted TEXT NOT NULL,
    scopes                 TEXT[] NOT NULL DEFAULT '{}',
    push_secret            TEXT,
    last_refreshed_at      TIMESTAMPTZ,
    expires_at             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connector_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_oauth_tokens_expires
    ON public.tenant_oauth_tokens (expires_at)
    WHERE expires_at IS NOT NULL;

ALTER TABLE public.tenant_oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_oauth_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_oauth_tokens_select ON public.tenant_oauth_tokens;
CREATE POLICY tenant_oauth_tokens_select ON public.tenant_oauth_tokens
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_oauth_tokens_insert ON public.tenant_oauth_tokens;
CREATE POLICY tenant_oauth_tokens_insert ON public.tenant_oauth_tokens
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_oauth_tokens_update ON public.tenant_oauth_tokens;
CREATE POLICY tenant_oauth_tokens_update ON public.tenant_oauth_tokens
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_oauth_tokens_delete ON public.tenant_oauth_tokens;
CREATE POLICY tenant_oauth_tokens_delete ON public.tenant_oauth_tokens
    FOR DELETE USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_oauth_tokens_operator_select ON public.tenant_oauth_tokens;
CREATE POLICY tenant_oauth_tokens_operator_select ON public.tenant_oauth_tokens
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE public.tenant_oauth_tokens IS
    'VT-207 OAuth tokens. Encrypted refresh_token at rest via VT-191 Fernet substrate. Phase-1 PK is (tenant_id, connector_id); multi-instance support requires instance_id column (see migration header).';
COMMENT ON COLUMN public.tenant_oauth_tokens.push_secret IS
    'HMAC secret for Apps Script push verification (VT-207 sheet push path). Per-(tenant,connector) — Phase-1.';
