-- 053_skipped_send_status.sql — VT-261: add 'skipped' to the send_status CHECKs.
--
-- VT-251's opt-out skip path recorded send_status='error' because no 'skipped'
-- value existed in the CHECK — polluting error telemetry and conflating
-- deliberate consent skips (opted_out / blocked recipients we intentionally do
-- NOT message, per CL-421) with real send failures. Add 'skipped' to both
-- outbound-ledger tables so the execution seam can record skips honestly.
--
-- No data backfill: existing 'error' rows from prior skips are left as-is (low
-- volume, dev-only synthetic per CL-422). The seam writes 'skipped' going forward.

ALTER TABLE public.send_idempotency_keys
    DROP CONSTRAINT send_idempotency_keys_send_status_check;
ALTER TABLE public.send_idempotency_keys
    ADD CONSTRAINT send_idempotency_keys_send_status_check
    CHECK (send_status IN ('sent', 'window_closed', 'rate_limited', 'error', 'skipped'));

ALTER TABLE public.campaign_messages
    DROP CONSTRAINT campaign_messages_send_status_check;
ALTER TABLE public.campaign_messages
    ADD CONSTRAINT campaign_messages_send_status_check
    CHECK (send_status IN ('sent', 'window_closed', 'rate_limited', 'error', 'template_sent', 'skipped'));
