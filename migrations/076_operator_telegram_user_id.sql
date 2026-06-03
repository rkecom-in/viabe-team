-- 076_operator_telegram_user_id.sql — VT-297 inbound Telegram identity binding.
--
-- VT-298 (mig 075) bound an operator to an OUTBOUND chat_id (where alerts are pushed). VT-297
-- adds the INBOUND identity: a Telegram user_id (the attacker-controllable `message.from.id` on
-- every update) maps to a VERIFIED operator. This column is the ONLY thing that turns an inbound
-- update into an authenticated VTR — resolution is `telegram_user_id → operator_id` and ONLY when
-- the row is verified (verified_at IS NOT NULL). An unverified / unknown user_id resolves to
-- NOTHING (fail-closed; the IDOR-equivalent for this surface).
--
-- telegram_user_id is a 64-bit int (Telegram user ids exceed int4), distinct from chat_id.
-- Partial-UNIQUE on the VERIFIED rows prevents two operators sharing one verified Telegram
-- account (account-takeover guard). Deny-all FORCE RLS unchanged (service-role only; the bot
-- reads/writes via the team-web service-role client, app-side scoping).
--
-- Verification: the VTR-authed web issuer page mints a single-use code into verification_code
-- (mig 075); `/link <code>` from Telegram matches it, stamps verified_at + stores this
-- telegram_user_id. CL-422 synthetic on dev. Migration 076 via the allocator (CL-424).

ALTER TABLE public.operator_telegram
    ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT NULL;

-- One operator per VERIFIED telegram account (and vice-versa): a verified telegram_user_id binds
-- to exactly one operator. Partial so unverified/NULL rows don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_telegram_user_verified
    ON public.operator_telegram (telegram_user_id)
    WHERE telegram_user_id IS NOT NULL AND verified_at IS NOT NULL;
