-- 139_vt420_sending_inflight_marker.sql — VT-420: close the Twilio-success→ledger-'sent'
-- double-send crash window with a pre-send 'sending' (in-flight) ledger marker.
--
-- The win-back send path (send_whatsapp_template, shared by L2 + L3 via
-- agents/customer_send.agent_send_draft) has a bounded double-send residual: the
-- Twilio messages.create call and the autocommit idempotency-ledger 'sent' INSERT are
-- NOT one transaction (the pool is autocommit). A crash AFTER Twilio delivers but
-- BEFORE the 'sent' row commits left the key absent → on recovery the send re-fired
-- (double-charge / double-message). Twilio's Messages/Content API has NO native
-- idempotency key (confirmed: twilio 9.10.9 messages.create exposes none, and the
-- official Messages REST docs document none — the I-Twilio-Idempotency-Token header
-- is a WEBHOOK construct, the reverse direction), so the money-SAFE fix is a pre-send
-- 'sending' marker: write+commit 'sending' BEFORE messages.create, flip it to 'sent'
-- after. On recovery a 'sending' marker with no 'sent' means the message was PROBABLY
-- already dispatched → do NOT re-send (converts a double-send into a recoverable
-- possible-missed-send — never a double-charge).
--
-- This migration adds 'sending' to the send_idempotency_keys send_status CHECK.
-- campaign_messages is the post-send audit record and never carries 'sending', so its
-- CHECK is intentionally left unchanged.
--
-- Idempotent re-assert (DROP CONSTRAINT IF EXISTS) per the migration-057 pattern.
-- CL-422: dev holds synthetic data only until prod-in-Mumbai (VT-231); no backfill.
-- Migration number 139 allocated via scripts/migration_id_allocate.py (CL-424).

ALTER TABLE public.send_idempotency_keys
    DROP CONSTRAINT IF EXISTS send_idempotency_keys_send_status_check;
ALTER TABLE public.send_idempotency_keys
    ADD CONSTRAINT send_idempotency_keys_send_status_check
    CHECK (send_status IN ('sent', 'sending', 'window_closed', 'rate_limited', 'error', 'skipped'));
