-- VT-81 — Twilio inbound replay defense
--
-- One row per (MessageSid) within a 5-minute window. Lookup-then-insert
-- under ON CONFLICT DO NOTHING gives idempotent replay rejection.
-- 5-min window vs Twilio's ~24h retry budget covers the realistic
-- replay-attack vector without DB bloat. Cleanup via TTL index or
-- scheduled purge (separate row).

CREATE TABLE IF NOT EXISTS public.twilio_inbound_replay (
    message_sid TEXT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip TEXT NOT NULL,
    signature_first_8 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_twilio_inbound_replay_received_at
    ON public.twilio_inbound_replay (received_at);
