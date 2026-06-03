-- 077_telegram_update_replay.sql — VT-297 inbound Telegram replay guard.
--
-- Telegram re-delivers an update on a slow 200, and a captured valid update (post-secret) could be
-- replayed. update_id is Telegram's per-bot monotonic idempotency key. The webhook does an atomic
-- INSERT ... ON CONFLICT DO NOTHING here BEFORE acting on any state-changing command (/link,
-- /ack, /resolve); a duplicate update_id → no-op + 200. Mirrors twilio_inbound_replay.
--
-- Bot-global (not tenant-scoped — update_id is unique per bot, identity is resolved later).
-- Deny-all FORCE RLS: service-role only (the team-web webhook uses serverSecretClient). CL-422
-- synthetic on dev. Migration 077 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.telegram_update_replay (
    update_id    BIGINT PRIMARY KEY,            -- Telegram's per-bot monotonic update id
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Housekeeping: prune old rows by received_at (a scheduled sweep can DELETE < now()-interval).
CREATE INDEX IF NOT EXISTS idx_telegram_update_replay_received
    ON public.telegram_update_replay (received_at);

ALTER TABLE public.telegram_update_replay ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_update_replay FORCE ROW LEVEL SECURITY;
