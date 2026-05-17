-- 003_subscriptions.sql — Razorpay subscription state per tenant.
CREATE TABLE subscriptions (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  UUID NOT NULL REFERENCES tenants (id),
    razorpay_subscription_id   TEXT UNIQUE,
    razorpay_plan_id           TEXT,
    status                     TEXT,
    started_at                 TIMESTAMPTZ,
    cumulative_fees_paid_paise BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX subscriptions_tenant_idx ON subscriptions (tenant_id);

-- Pillar 3: tenant-scoped RLS, same migration.
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;

CREATE POLICY subscriptions_select ON subscriptions FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY subscriptions_insert ON subscriptions FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY subscriptions_update ON subscriptions FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY subscriptions_delete ON subscriptions FOR DELETE
    USING (tenant_id = app_current_tenant());
