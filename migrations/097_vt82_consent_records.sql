-- VT-82 — owner signup consent proof.
--
-- DISTINCT from the phone-token-keyed CUSTOMER consent (`record_of_consent`,
-- mig 067 / CL-425). This is the BUSINESS OWNER's signup consent: DPDPA + data-
-- residency, captured once at signup as legal proof of lawful processing.
--
-- DSR posture (Cowork-required, VT-323/VT-325 lesson): consent_records is
-- DELIBERATELY NOT in dsr_purge._PURGE_ORDER — it is RETAINED on a tenant DSR
-- (which anonymizes the tenant, does not delete it), exactly like privacy_audit_log
-- (DPDP defensibility: we keep proof that consent was lawfully obtained). This is
-- safe because the row is PII-FREE: tenant_id + two booleans + version strings +
-- timestamp, with NO name/phone/email. The retention is verified by a real-PG
-- canary asserting it SURVIVES the purge while a co-resident tenant is untouched.
--
-- The dpdpa_version / residency_version strings reference the actual disclosure
-- docs (config/disclosure_versions.yaml), not free text.
--
-- Writes happen at signup PRE-tenant-context (service_role pool, no GUC) — the
-- RLS policies below are for the app_role READ path; the FORCE-RLS + tenant policy
-- scopes any later in-tenant read.

CREATE TABLE IF NOT EXISTS public.consent_records (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    consent_dpdpa      BOOLEAN NOT NULL,
    consent_residency  BOOLEAN NOT NULL,
    dpdpa_version      TEXT NOT NULL,
    residency_version  TEXT NOT NULL,
    signed_up_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_consent_records_tenant
    ON public.consent_records (tenant_id);

ALTER TABLE public.consent_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.consent_records FORCE ROW LEVEL SECURITY;

CREATE POLICY consent_records_select ON public.consent_records
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY consent_records_insert ON public.consent_records
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY consent_records_update ON public.consent_records
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY consent_records_delete ON public.consent_records
    FOR DELETE USING (tenant_id = app_current_tenant());
