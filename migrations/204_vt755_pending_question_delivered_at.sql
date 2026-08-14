-- 204_vt755_pending_question_delivered_at.sql — VT-755: a question the owner never received
-- must not be answerable.
--
-- THE DEFECT THIS COLUMN EXISTS TO CLOSE, measured on deployed dev 2026-08-14:
--
--   12:19:13  owner      purane customers ko wapas laane ke liye ek accha sa offer draft kar do
--   12:21:47  assistant  Got it — I'm on it and I'll update you shortly.
--   12:23:35  owner      haan theek hai, bhej do unhe          <-- "yes fine, SEND IT TO THEM"
--   12:28:25  assistant  Got it — I'm on it and I'll update you shortly.
--
-- Meanwhile `pending_questions` held a question asked at 12:19:58 that appears NOWHERE in
-- conversation_log — `pending_questions` has no emitter, so nothing ever sent it. At 12:26:07
-- `correlate_reply` bound the owner's 12:23:35 message to that invisible question and stamped it
-- `answered`. The owner's actual INSTRUCTION was consumed as clarification and discarded.
--
-- `correlate_reply` selects the oldest open question for the tenant with no notion of whether the
-- question was ever delivered, and `get_open` — which decides whether the turn routes to
-- `answer_pending` at all — has the same blind spot. So any owner message can be swallowed by a
-- question they never saw. Observed on 4 of 4 stalled tenants in the same re-drive.
--
-- WHAT THIS COLUMN CHANGES TODAY. There is still no emitter (that fix routes through the single
-- Manager emission choke and is a design call — VT-755 scope 0), so `delivered_at` stays NULL for
-- every row, `get_open` returns nothing, and an owner message falls through to normal dispatch —
-- where "bhej do unhe" is read as the send instruction it is. That is the correct behaviour while
-- the ask is undeliverable. When the emitter lands it stamps `delivered_at` and correlation resumes
-- for exactly the questions the owner actually received.
--
-- Additive and nullable: no backfill, and every existing row is treated as UNDELIVERED, which is the
-- truthful reading — none of them were sent.
--
-- Migration 204 via the allocator (CL-424). RLS/FORCE on the table are unchanged; no new policy is
-- needed for a new column on an existing tenant-scoped table.

ALTER TABLE public.pending_questions
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;

COMMENT ON COLUMN public.pending_questions.delivered_at IS
    'VT-755: when this question was actually EMITTED to the owner. NULL = never sent, and an unsent '
    'question must never be treated as answerable — correlate_reply and get_open both require it to '
    'be non-NULL, so an owner message can no longer be swallowed as the answer to a question they '
    'never saw. Set by the emission path; enforced by '
    'tests/orchestrator/manager/test_vt755_undelivered_question_cannot_be_answered.py.';

-- Partial index on the correlation predicate: the hot path asks "does this tenant have a DELIVERED
-- open question?" on every inbound turn.
CREATE INDEX IF NOT EXISTS pending_questions_open_delivered_idx
    ON public.pending_questions (tenant_id, asked_at)
    WHERE status = 'open' AND delivered_at IS NOT NULL;
