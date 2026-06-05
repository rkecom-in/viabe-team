-- 106_vt335_template_error_reports.sql — VT-335 (VT-84 PR-2): owner template-error reports.
--
-- The owner reports a message we sent that was wrong/broken. owner_complaint is the
-- owner's FREE TEXT — stored minimally for Fazal's diagnosis. It is PII and MAY name a
-- customer (third-party PII).
--
-- KNOWN LIMITATION (Cowork 20260605T165000Z): a customer named in the free text is NOT
-- reachable by THAT customer's DSR — the row is TENANT-keyed and hard-deleted only on the
-- TENANT's DSR. No free-text PII extraction at launch; tracked here + in the runbook so
-- it is not silent. Hard-delete on DSR (a support complaint has NO financial/tax retention
-- obligation, so NOT anonymize-retain) — in dsr_purge._PURGE_ORDER after founding_tier_claims.
CREATE TABLE template_error_reports (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants (id),
    owner_complaint    TEXT NOT NULL,
    recent_template_id TEXT,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'resolved', 'dismissed')),
    reported_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX template_error_reports_tenant_idx ON template_error_reports (tenant_id);

ALTER TABLE template_error_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE template_error_reports FORCE ROW LEVEL SECURITY;
-- App-role reads its own tenant; the handler insert + DSR purge are service-role (BYPASSRLS).
CREATE POLICY template_error_reports_select ON template_error_reports
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY template_error_reports_insert ON template_error_reports
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
