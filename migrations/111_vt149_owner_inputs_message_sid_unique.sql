-- 111_vt149_owner_inputs_message_sid_unique.sql — VT-149: replay idempotency at the row level.
--
-- Promote owner_inputs (tenant_id, message_sid) from a non-unique provenance index (mig 020) to
-- a UNIQUE one, so a DBOS webhook_pipeline_run REPLAY does not write a SECOND owner_inputs row
-- for the same originating WhatsApp message. (Operational correctness, NOT privacy — normal
-- priority; deliberately separate from the workflow_inputs retention concern.)
--
-- DEDUPE-THEN-CONSTRAIN (data-altering — routed for review, not a pure-additive migration):
-- a replay BEFORE this guard could already have written duplicate (tenant_id, message_sid) rows,
-- so a bare CREATE UNIQUE would fail. Delete the duplicates FIRST, keeping the EARLIEST row per
-- key (the first real write; later replays are the noise). message_sid IS NULL rows (owner NL
-- entry, non-Twilio) have no dedup key and are left untouched.

-- 1. Dedupe: delete the strictly-later duplicate(s) per (tenant_id, message_sid); keep earliest.
DELETE FROM owner_inputs a
USING owner_inputs b
WHERE a.message_sid IS NOT NULL
  AND a.tenant_id = b.tenant_id
  AND a.message_sid = b.message_sid
  AND (a.created_at, a.id) > (b.created_at, b.id);

-- 2. Replace the non-unique provenance index with a UNIQUE index (same partial predicate).
DROP INDEX IF EXISTS owner_inputs_tenant_message_sid;
CREATE UNIQUE INDEX owner_inputs_tenant_message_sid
    ON owner_inputs (tenant_id, message_sid)
    WHERE message_sid IS NOT NULL;
