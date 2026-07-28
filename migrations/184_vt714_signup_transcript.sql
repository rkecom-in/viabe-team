-- 184 (VT-714) — capture the PRE-TENANT signup conversation and let it flush into the
-- tenant's lifetime conversation_log once the tenant exists.
--
-- Fazal 2026-07-28 (run reviews): "There is no mention or flow history of where did the
-- actual onboarding start from. Journeys must be captured even when the user is new,
-- not-logged in state … and those action must be flagged as non-logged in state."
--
-- 1. whatsapp_signup_sessions.transcript — the pre-tenant turns ({role, text, sid, ts}
--    entries, appended by the signup state machine). Lives ON the session row so it
--    inherits the existing DPDP retention posture: declined/expired sessions are purged by
--    purge_stale() and their transcripts go with them; nothing outlives the session unless
--    consent converts it into a tenant.
-- 2. conversation_log.surface gains 'signup' — the flag for turns that happened BEFORE the
--    tenant existed (pre-login state). The flush writes them with their ORIGINAL timestamps.

ALTER TABLE whatsapp_signup_sessions
    ADD COLUMN IF NOT EXISTS transcript jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE conversation_log DROP CONSTRAINT IF EXISTS conversation_log_surface_check;
ALTER TABLE conversation_log
    ADD CONSTRAINT conversation_log_surface_check
    CHECK (surface IN ('journey', 'manager', 'system', 'signup'));
