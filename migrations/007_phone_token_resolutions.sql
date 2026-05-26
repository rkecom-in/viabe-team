-- 007_phone_token_resolutions.sql — phone-number tokenisation (Pillar 7).
--
-- VT-178 docstring amendment (2026-05-26): this table originally had column
-- shape `token, tenant_id, phone_number_encrypted, resolved_count,
-- last_resolved_at, created_at`. VT-178 added composite index
-- `phone_token_resolutions_tenant_token_idx` via migration 024.
--
-- VT-187 docstring amendment (2026-05-26): schema normalized to §2.1 spec
-- via `025_pipeline_observability_normalize.sql`. Renamed `token` →
-- `phone_token`, `last_resolved_at` → `last_accessed_at`. Added canonical
-- column `customer_id` (NO FK constraint per CL-417 Cond 1 — TODO(VT-170)
-- adds the customers table and the FK). PostgreSQL auto-updated the
-- primary-key constraint and `phone_token_resolutions_tenant_token_idx` to
-- reference the renamed `phone_token` column. See CL-417 for rationale.
--
-- RLS is the SAME as the other tenant-scoped tables (4 policies via
-- `app_current_tenant()`). Brief §2.1 calls for STRICTER RLS — operator-
-- role required for resolution, NOT just tenant role. That stricter
-- substrate (new `app_operator_role` + `tenant_connection_operator()`
-- wrapper) deferred to VT-188 (Cowork files post-VT-178; required
-- before VT-123 Ops UI ships).
--
-- Phone numbers are the one PII field stored encrypted. Everything else
-- references the opaque token (format: cust_tok_<hash>). The write path,
-- encryption, and resolution logic are built in VT-8 — this migration only
-- creates the table so VT-122 and VT-3.3 can write against a stable schema.
CREATE TABLE phone_token_resolutions (
    token                  TEXT PRIMARY KEY,
    tenant_id              UUID NOT NULL REFERENCES tenants (id),
    phone_number_encrypted TEXT,
    resolved_count         INT NOT NULL DEFAULT 0,
    last_resolved_at       TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX phone_token_resolutions_tenant_idx ON phone_token_resolutions (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration.
ALTER TABLE phone_token_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE phone_token_resolutions FORCE ROW LEVEL SECURITY;

CREATE POLICY phone_token_resolutions_select ON phone_token_resolutions FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY phone_token_resolutions_insert ON phone_token_resolutions FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY phone_token_resolutions_update ON phone_token_resolutions FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY phone_token_resolutions_delete ON phone_token_resolutions FOR DELETE
    USING (tenant_id = app_current_tenant());
