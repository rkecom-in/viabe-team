-- 007_phone_token_resolutions.sql — phone-number tokenisation (Pillar 7).
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
