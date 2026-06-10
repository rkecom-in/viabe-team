-- 121_vt365_remove_refund_subsystem.sql — VT-365 (Fazal 2026-06-09): 30-day free trial, NO card in
-- trial, opt-in subscribe at day 30, NO auto-charge, NO refund ever. Retires the refund subsystem +
-- trial extensions at the schema level:
--   1. maps any existing rows OUT of the retiring phases (the CHECK reshape would fail otherwise)
--   2. reshapes the tenants + subscriber_states phase CHECK to the new set (adds 'lapsed', drops
--      'trial_extended','refund_offered','refunded') — both kept in sync (CL-428 phase-literal sync)
--   3. drops the refund_executions (VT-93) + day39_evaluations (VT-92) tables
--   4. drops the now-dead tenants.refunded_at + trial_extension_count columns
-- Forward-only. Runs guarded via apply_migrations.py --expected-env (dev now; prod on promotion;
-- Mumbai prod has only the founding onboarding tenant, so the phase-map UPDATEs are no-ops there).

-- 1. Map existing rows out of the retiring phases (valid under the OLD check; required before the
--    reshape). refunded -> cancelled (terminal), refund_offered -> paid_active (was paid),
--    trial_extended -> trial.
UPDATE public.tenants          SET phase = 'cancelled'   WHERE phase = 'refunded';
UPDATE public.tenants          SET phase = 'paid_active' WHERE phase = 'refund_offered';
UPDATE public.tenants          SET phase = 'trial'       WHERE phase = 'trial_extended';
UPDATE public.subscriber_states SET phase = 'cancelled'   WHERE phase = 'refunded';
UPDATE public.subscriber_states SET phase = 'paid_active' WHERE phase = 'refund_offered';
UPDATE public.subscriber_states SET phase = 'trial'       WHERE phase = 'trial_extended';

-- 2. Reshape the phase CHECK on BOTH tables (kept in sync — CL-428).
ALTER TABLE public.tenants DROP CONSTRAINT tenants_phase_check;
ALTER TABLE public.tenants ADD CONSTRAINT tenants_phase_check
    CHECK (phase IN ('onboarding', 'trial', 'lapsed', 'paid_active', 'paid_at_risk', 'cancelled'));

ALTER TABLE public.subscriber_states DROP CONSTRAINT subscriber_states_phase_check;
ALTER TABLE public.subscriber_states ADD CONSTRAINT subscriber_states_phase_check
    CHECK (phase IN ('onboarding', 'trial', 'lapsed', 'paid_active', 'paid_at_risk', 'cancelled'));

-- 3. Drop the refund subsystem tables (VT-92 day-39 evaluator + VT-93 refund executor).
DROP TABLE IF EXISTS public.refund_executions;
DROP TABLE IF EXISTS public.day39_evaluations;

-- 4. Drop the now-dead columns (no refunds, no extensions).
ALTER TABLE public.tenants          DROP COLUMN IF EXISTS refunded_at;
ALTER TABLE public.tenants          DROP COLUMN IF EXISTS trial_extension_count;
ALTER TABLE public.subscriber_states DROP COLUMN IF EXISTS trial_extension_count;
