-- 066_tenant_business_contact_identity.sql — VT-267 PR-A2 (Fazal D1, 2026-06-02).
--
-- Fazal D1 ruling: the business_contact (the tenant's WhatsApp number) is the
-- MANDATORY TENANT IDENTITY → globally UNIQUE; owner_contact is OPTIONAL
-- (escalation/severe-interaction only, nullable, NOT unique). Dual entry (web-link
-- OR inbound WhatsApp) both resolve to the business number as the tenant key —
-- same number → SAME tenant (merge, not new).
--
-- SUPERSEDES CL-76 DC2: migration 014 DELIBERATELY left whatsapp_number NON-unique
-- (a plain index, tenants_whatsapp_number_idx) so _lookup_tenant could use
-- "most-recent-wins" semantics over possible duplicates. Fazal's D1 reverses that —
-- the number is now the unique identity, so duplicates are disallowed. (_lookup_
-- tenant's ORDER BY created_at DESC LIMIT 1 stays harmless under the unique index.)
--
-- Partial unique (WHERE whatsapp_number IS NOT NULL): legacy rows with a NULL
-- number are exempt. A pre-existing duplicate would make the index build fail
-- loudly — intended (dev is synthetic, CL-422; a fresh migrate has none).
-- Migration 066 via scripts/migration_id_allocate.py (CL-424).

ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS owner_contact TEXT NULL;

DROP INDEX IF EXISTS tenants_whatsapp_number_idx;

CREATE UNIQUE INDEX IF NOT EXISTS tenants_whatsapp_number_key
    ON public.tenants (whatsapp_number)
    WHERE whatsapp_number IS NOT NULL;
