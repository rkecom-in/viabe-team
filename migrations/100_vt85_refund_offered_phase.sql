-- 100_vt85_refund_offered_phase.sql — VT-85: refund-offer phase for the day-39
-- refund-conversation engine.
--
-- VT-92 landed the day-39 evaluator + an AUTO-transition to 'refunded' on the
-- refund verdict. VT-85 converts that auto-refund into an OFFER (Pillar 7 honest
-- framing): the evaluator sends a refund_offer and parks the tenant in the new
-- 'refund_offered' phase until the owner replies REFUND / CONTINUE / DISCUSS — or
-- the 48h timeout defaults to CONTINUE (auto-refund without consent is financially
-- destabilizing). The actual refund (VT-93 execute_refund) fires only on REFUND.
--
-- Adds 'refund_offered' to the phase CHECK on BOTH tenants + subscriber_states.
-- apply_transition (transitions.py) mirrors phase across the two rows, so the two
-- CHECK lists MUST stay in sync (phase-literal sync, CL-428 discipline for phases).
-- The transitions themselves + the day39_refund_offered / day39_refund_decision
-- event kinds are code (TRANSITIONS dict + EVENT_SCHEMAS), not this migration.

ALTER TABLE public.tenants DROP CONSTRAINT tenants_phase_check;
ALTER TABLE public.tenants ADD CONSTRAINT tenants_phase_check
    CHECK (phase IN (
        'onboarding', 'trial', 'trial_extended',
        'paid_active', 'paid_at_risk', 'refund_offered',
        'cancelled', 'refunded'));

ALTER TABLE public.subscriber_states DROP CONSTRAINT subscriber_states_phase_check;
ALTER TABLE public.subscriber_states ADD CONSTRAINT subscriber_states_phase_check
    CHECK (phase IN (
        'onboarding', 'trial', 'trial_extended',
        'paid_active', 'paid_at_risk', 'refund_offered',
        'cancelled', 'refunded'));
