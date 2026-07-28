-- 185 (VT-715) — owner-email substrate + the DPDP consent-record email stamp.
--
-- Fazal 2026-07-28: "When we get the DPDP consent and privacy policy agreed by the tenant,
-- either from the UI or through WhatsApp, in both cases we must send out an email to the
-- tenant that will work as a record of the consent."
--
-- tenants.owner_email — the FIRST owner-email substrate in the product (monthly reports have
-- passed owner_email=None since VT-86). NULL until a capture surface exists; the consent-
-- record email fires immediately when present, and a pending record (record_email_sent_at
-- IS NULL) fires retroactively the moment an email is captured.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS owner_email TEXT NULL;

ALTER TABLE consent_records
    ADD COLUMN IF NOT EXISTS record_email_sent_at timestamptz NULL;
