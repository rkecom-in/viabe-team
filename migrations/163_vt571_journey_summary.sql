-- 163_vt571_journey_summary.sql — VT-571: the conversation memory must COMPACT, not drop.
--
-- FOLLOW-ON TO 162 (Fazal, live drill, binding: "an evolving and compacting memory"). Migration 162
-- gave the turn brain a rolling transcript window (recent_turns), but that window is a HARD cap-8: the
-- moment turn 9 lands, turn 1 is silently EVICTED and gone forever. So a durable fact the owner stated
-- early (a decision, a preference, an open thread) simply falls out of memory once the chat runs long —
-- the same "amnesia" class of defect 162 set out to fix, just deferred by eight turns.
--
-- THIS COLUMN is the running DISTILLED summary. When turns overflow the recent_turns window, the
-- evicted head is NOT dropped — it is folded (one Haiku call, off the hot path) into this compact
-- memory of durable facts/decisions/preferences/open threads. The turn brain reads it ABOVE the raw
-- recent window, so "what was said 20 turns ago" survives as memory even after it leaves the transcript.
--
-- Tenant-scoped: rides mig-123's RLS+FORCE on onboarding_journey; erased with the row on DSR (this is
-- distilled from the tenant's own onboarding chat — same data class as `recent_turns` / `answers`).
--
-- Idempotent.

ALTER TABLE onboarding_journey
    ADD COLUMN IF NOT EXISTS conversation_summary TEXT NULL;

COMMENT ON COLUMN onboarding_journey.conversation_summary IS
    'VT-571: the running DISTILLED memory — durable facts/decisions/preferences/open threads folded from turns evicted out of recent_turns (compact, not drop). Tenant-scoped; DSR-erased with the row.';
