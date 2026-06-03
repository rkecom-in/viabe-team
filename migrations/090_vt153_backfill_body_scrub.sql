-- 090_vt153_backfill_body_scrub.sql — VT-153 pre-#45 plaintext-body backfill.
--
-- VT-144 (#45) stopped the raw WhatsApp `body` (+ aliases) from persisting
-- FORWARD via runner._redact_for_persistence; it did NOT scrub the historical
-- rows written before #45. This migration scrubs that existing population:
-- an idempotent JSONB top-level key-strip of the VT-144 redaction family
-- (`body, message_body, raw_text, content`) from the persisted envelopes.
--
-- Surface (matches VT-144's redaction boundary + the VT-79 PII-detector scan):
--   - pipeline_runs.trigger_payload
--   - pipeline_steps.input_envelope
--   - pipeline_steps.output_envelope (defensive — runner never writes body here,
--     but the PII-detector scans it; the `?|` guard makes it a 0-row no-op if clean)
--
-- Idempotency: the `?|` (has-any-key) guard means each UPDATE touches ONLY rows
-- that still carry a redaction-family key → safe + cheap to re-run (0 rows the
-- second time). NULL / non-object payloads never match `?|`, so they are skipped.
--
-- FTS: pipeline_steps.envelope_search_tsv is GENERATED ALWAYS ... STORED
-- (mig 038) over input_envelope::text || output_envelope::text — so these
-- UPDATEs auto-recompute the tsvector from the SCRUBBED envelopes, removing the
-- body tokens from the GIN-indexed search column too. No separate FTS refresh.
--
-- CL-390 (no raw body at rest) · CL-422 (dev synthetic-only; on prod this scrubs
-- the real pre-#45 population at deploy, pre-VT-231). Claimed via
-- scripts/migration_id_allocate.py (CL-424). VT-147 retired as a duplicate.

UPDATE public.pipeline_runs
   SET trigger_payload = trigger_payload - 'body' - 'message_body' - 'raw_text' - 'content'
 WHERE trigger_payload ?| array['body', 'message_body', 'raw_text', 'content'];

UPDATE public.pipeline_steps
   SET input_envelope = input_envelope - 'body' - 'message_body' - 'raw_text' - 'content'
 WHERE input_envelope ?| array['body', 'message_body', 'raw_text', 'content'];

UPDATE public.pipeline_steps
   SET output_envelope = output_envelope - 'body' - 'message_body' - 'raw_text' - 'content'
 WHERE output_envelope ?| array['body', 'message_body', 'raw_text', 'content'];
