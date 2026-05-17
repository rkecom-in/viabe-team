-- 001_tenants.sql — tenants: registry of businesses on Viabe Team.
CREATE TABLE tenants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name       TEXT NOT NULL,
    locality            TEXT,
    business_type       TEXT,
    city_tier           TEXT,
    language_preference TEXT NOT NULL DEFAULT 'en',
    signed_up_at        TIMESTAMPTZ,
    plan_tier           TEXT NOT NULL CHECK (plan_tier IN ('founding', 'standard', 'pro')),
    phase               TEXT NOT NULL CHECK (phase IN (
                            'onboarding', 'trial', 'trial_extended',
                            'paid_active', 'paid_at_risk', 'cancelled', 'refunded')),
    phase_entered_at    TIMESTAMPTZ,
    whatsapp_number     TEXT,
    preferred_language  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pillar 3: RLS lives in the same migration that creates the table.
-- The tenant row is keyed on its own id.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

CREATE POLICY tenants_select ON tenants FOR SELECT
    USING (id = app_current_tenant());
CREATE POLICY tenants_insert ON tenants FOR INSERT
    WITH CHECK (id = app_current_tenant());
CREATE POLICY tenants_update ON tenants FOR UPDATE
    USING (id = app_current_tenant())
    WITH CHECK (id = app_current_tenant());
CREATE POLICY tenants_delete ON tenants FOR DELETE
    USING (id = app_current_tenant());
