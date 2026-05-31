-- 057_skipped_check_idempotent.sql — VT-265: make migration 053's send_status
-- CHECK re-assertions re-runnable.
--
-- Migration 053 (skipped_send_status) used bare `ALTER TABLE ... DROP CONSTRAINT
-- <name>` without `IF EXISTS` — the only migration that does so. The runner
-- tracks by name so 053 won't re-run on a healthy DB, but a partial/re-run would
-- fail on the already-dropped constraint. 053 is name-tracked and already
-- applied; editing it would be inert + misleading. Instead, re-assert both
-- CHECKs idempotently here with `DROP CONSTRAINT IF EXISTS` so the constraint
-- definition is belt-and-braces re-runnable. Value sets are identical to the
-- current definitions on main (053) — no behavioural change.

ALTER TABLE public.send_idempotency_keys
    DROP CONSTRAINT IF EXISTS send_idempotency_keys_send_status_check;
ALTER TABLE public.send_idempotency_keys
    ADD CONSTRAINT send_idempotency_keys_send_status_check
    CHECK (send_status IN ('sent', 'window_closed', 'rate_limited', 'error', 'skipped'));

ALTER TABLE public.campaign_messages
    DROP CONSTRAINT IF EXISTS campaign_messages_send_status_check;
ALTER TABLE public.campaign_messages
    ADD CONSTRAINT campaign_messages_send_status_check
    CHECK (send_status IN ('sent', 'window_closed', 'rate_limited', 'error', 'template_sent', 'skipped'));
