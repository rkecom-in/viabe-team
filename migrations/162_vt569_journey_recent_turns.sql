-- 162_vt569_journey_recent_turns.sql — VT-569 follow-up: conversation memory for the turn brain.
--
-- LIVE-DRILL DEFECT (Fazal, 2026-07-03 ~02:15 IST): the owner confirmed a business description the
-- BOT itself had proposed ("should I use that — AI-powered business intelligence…?" → "Use that").
-- Nothing recorded it: the extraction rule (correctly) only allows values the owner literally typed
-- THIS message, and confirm-promotion only falls back to DISCOVERED draft values (about was null) —
-- a value born IN THE CONVERSATION fell through both. With no transcript memory, every later turn
-- re-derived wrong guesses and re-asked — the "annoying agent" failure.
--
-- THIS COLUMN is the rolling short transcript window (last ~8 {role, text, at} entries, owner + bot),
-- persisted on the journey row so the turn brain sees what IT said last turn and what the owner
-- affirmed. Tenant-scoped: rides mig-123's RLS+FORCE on onboarding_journey; erased with the row on
-- DSR (owner text here is the tenant's own onboarding chat — same class as the `answers` column).
--
-- Idempotent.

ALTER TABLE onboarding_journey
    ADD COLUMN IF NOT EXISTS recent_turns JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN onboarding_journey.recent_turns IS
    'VT-569: rolling last-N conversation window [{role: owner|bot, text, at}] — the turn brain''s memory. Capped in code (~8); tenant-scoped; DSR-erased with the row.';
