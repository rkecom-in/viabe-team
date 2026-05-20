-- 017_subscriber_states.sql — persisted subscriber activity (VT-3.4 PR 3/3).
--
-- The SubscriberState TypedDict (orchestrator/state/__init__.py) is the
-- canonical runtime shape (VT-3.2). Until now nothing persisted its
-- non-phase fields — phase mirrors onto tenants via apply_transition, the
-- rest stayed in-memory only.
--
-- Minimal-by-design (CL-233): only the columns the PR-3/3 collapse path
-- writes. Phase is here so the row is consistent with the TypedDict, but
-- the collapse path NEVER mutates it (apply_transition remains the sole
-- phase mutator — Pillar 8). last_campaign_at + attribution_close_pending
-- are the activity fields the collapse path updates on each CampaignPlan.
--
-- One row per tenant: PK is tenant_id (current scope is single-subscriber-
-- per-tenant; multi-subscriber expansion lands in a later subtask).
CREATE TABLE subscriber_states (
    tenant_id                 UUID PRIMARY KEY REFERENCES tenants (id),
    phase                     TEXT NOT NULL CHECK (phase IN (
                                  'onboarding', 'trial', 'trial_extended',
                                  'paid_active', 'paid_at_risk',
                                  'cancelled', 'refunded')),
    last_campaign_at          TIMESTAMPTZ,
    attribution_close_pending UUID[] NOT NULL DEFAULT '{}'
);

-- Pillar 3: RLS lives in the same migration that creates the table.
ALTER TABLE subscriber_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriber_states FORCE ROW LEVEL SECURITY;

CREATE POLICY subscriber_states_select ON subscriber_states FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY subscriber_states_insert ON subscriber_states FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY subscriber_states_update ON subscriber_states FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY subscriber_states_delete ON subscriber_states FOR DELETE
    USING (tenant_id = app_current_tenant());
