-- 123_vt367_onboarding_journey.sql — VT-367 Gap-3: the guided, paced onboarding journey.
--
-- Per-tenant CURSOR over the ordered question set (2b compose_onboarding_questions) — one question
-- "in flight", resumable across days, paced one-part-at-a-time over WhatsApp. DELIBERATELY separate
-- from subscriber_states (the Pillar-8 phase machine, single-mutator apply_transition): this is a
-- transient onboarding cursor, not lifecycle state. Holds owner-supplied business answers → RLS +
-- FORCE (the table is touched by the privileged owner pool, dsr_purge) AND in dsr_purge._PURGE_ORDER
-- from the START (the VT-366 2a bounce lesson — a new tenant table must be swept on DSR).

CREATE TABLE onboarding_journey (
    tenant_id        UUID PRIMARY KEY REFERENCES tenants (id),
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'complete', 'abandoned')),
    question_queue   JSONB NOT NULL DEFAULT '[]'::jsonb,   -- ordered 2b Question objects
    cursor           INTEGER NOT NULL DEFAULT 0,           -- index of the question in flight
    answers          JSONB NOT NULL DEFAULT '{}'::jsonb,   -- field -> owner value (confirmed/given)
    skipped          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- fields the owner skipped
    last_message_sid TEXT,                                 -- idempotency: last inbound advanced on
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);

-- Pillar 3: RLS in the same migration. FORCE so ownership alone can't bypass (dsr_purge hits it).
ALTER TABLE onboarding_journey ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_journey FORCE ROW LEVEL SECURITY;
CREATE POLICY onboarding_journey_select ON onboarding_journey FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY onboarding_journey_insert ON onboarding_journey FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY onboarding_journey_update ON onboarding_journey FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY onboarding_journey_delete ON onboarding_journey FOR DELETE
    USING (tenant_id = app_current_tenant());
