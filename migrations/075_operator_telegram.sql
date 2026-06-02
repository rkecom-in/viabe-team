-- 075_operator_telegram.sql — VT-298 VTR↔Telegram binding (chat_id + verification).
--
-- The autonomous watchdog (VT-202 detectors) must reach the ASSIGNED VTR on Telegram
-- immediately (Fazal: "report to VTR on Telegram immediately"). Recipient resolution is
-- tenant → assigned VTR(s) via operator_assignments (mig 072) → the VTR's VERIFIED Telegram
-- chat_id, stored here. Cowork DECISION 1 (2026-06-03): a SEPARATE table (not a column on
-- operator_allowlist) so a verify/opt-in step gates sends — we NEVER message an unverified
-- chat_id (verified_at IS NULL → not a send target).
--
-- Enforcement model (consistent with operator_allowlist 046 / operator_assignments 072):
-- deny-all FORCE RLS — only the service-role connection (orchestrator pool / team-web
-- serverSecretClient, both RLS-bypassing) touches it. The migration runner has no Supabase
-- auth/JWT context, so no JWT-claim RLS (VT-228 precedent).
--
-- No FK to auth.users (auth schema absent in the CI migrations runner). operator_id is a
-- bare UUID validated app-side (must be an active operator_allowlist row at bind time).
-- CL-422: synthetic on dev until VT-231. Migration 075 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.operator_telegram (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id     UUID NOT NULL,                 -- the VTR (operator_allowlist.user_id)
    chat_id         TEXT NOT NULL,                 -- Telegram chat id (string per Bot API)
    verified_at     TIMESTAMPTZ NULL,             -- non-NULL = verified; ONLY then a send target
    verification_code TEXT NULL,                   -- opt-in code the VTR echoes to verify
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- one binding per operator; re-bind UPSERTs the same row.
    CONSTRAINT operator_telegram_operator_uniq UNIQUE (operator_id)
);

-- Hot path: resolve an operator's VERIFIED chat_id for alert dispatch.
CREATE INDEX IF NOT EXISTS idx_operator_telegram_verified
    ON public.operator_telegram (operator_id)
    WHERE verified_at IS NOT NULL;

-- Deny-all RLS: no policies + FORCE → only the RLS-bypassing service role reaches it.
ALTER TABLE public.operator_telegram ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operator_telegram FORCE ROW LEVEL SECURITY;
