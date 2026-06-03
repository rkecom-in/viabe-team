-- 088_kg_events_pii_backfill.sql — VT-315 part B (CL-390 PII redaction).
--
-- The VT-65 PR-2 outbox is NOT deleted on drain (only drained_at stamped), so
-- raw PII emitted into kg_events.payload by pre-VT-315 code persists durably.
-- VT-315 part A stops NEW raw-PII writes (emit the phone HASH / real
-- business_name); this backfill cleans ALREADY-LANDED rows. The L1 KG already
-- holds the canonical hash_phone / tenant node, so the raw payload fields are
-- vestigial — redacting them is strictly privacy-improving + lossless to the KG.
--
-- Idempotent (the guards make a re-run a no-op). Runs on the real DB at VT-231;
-- dev rows are synthetic (CL-422). Claimed via scripts/migration_id_allocate.py.

-- 1. Drop raw phone_e164 from customer_created / customer_updated payloads.
UPDATE kg_events
   SET payload = payload - 'phone_e164'
 WHERE event_type IN ('customer_created', 'customer_updated')
   AND payload ? 'phone_e164';

-- 2. Drop phone-shaped business_name from tenant_created payloads (the tenant
--    node already carries it; only the raw-phone fallback case is redacted).
UPDATE kg_events
   SET payload = payload - 'business_name'
 WHERE event_type = 'tenant_created'
   AND payload ->> 'business_name' ~ '^\+?[0-9]{8,}$';
