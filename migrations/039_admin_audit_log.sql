-- VT-224 — admin audit log
--
-- Every call to /api/orchestrator/admin/* writes one row. token_fingerprint
-- is the first 8 chars of sha256(admin_token) — never the raw token.
-- Retention: lifetime-of-relationship per CL-416. DSR-purge path inherits
-- from tenant_id column (NULL for non-tenant-scoped calls; those persist
-- indefinitely as operational records).

CREATE TABLE IF NOT EXISTS public.admin_audit_log (
    id BIGSERIAL PRIMARY KEY,
    invoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    endpoint TEXT NOT NULL,
    tenant_id UUID,
    connector_id TEXT,
    source_ip TEXT NOT NULL,
    response_status INT NOT NULL,
    token_fingerprint TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS admin_audit_log_invoked_at_idx
    ON public.admin_audit_log (invoked_at DESC);

CREATE INDEX IF NOT EXISTS admin_audit_log_tenant_id_idx
    ON public.admin_audit_log (tenant_id, invoked_at DESC)
    WHERE tenant_id IS NOT NULL;
