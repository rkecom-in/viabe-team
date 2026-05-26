-- 023_attributions_and_cadence_columns.sql — schema substrate for VT-175.
--
-- Adds the `attributions` table + cadence columns on `campaigns` and `tenants`
-- so VT-176 can replace the shells in `scheduled_triggers.py` with real
-- bodies. CL-82 Standing: RLS via session GUC `app.current_tenant` (read by
-- the `app_current_tenant()` helper from 000b_rls_helpers.sql — substrate
-- convention across 20+ existing tables).
--
-- Pillar 3: RLS lives in the same migration that creates the table.
-- Pillar 1 (revised 2026-05-12): day-39 + attribution-close are
-- deterministic. NO LLM ever touches these rows.
-- CL-104 / Pillar 1 REVISED: `customer_id` NULLABLE until VT-170 ships the
-- customers table.

CREATE TABLE attributions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    campaign_id         UUID NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    customer_id         UUID NULL,
    razorpay_payment_id TEXT NULL,
    attributed_paise    BIGINT NOT NULL CHECK (attributed_paise >= 0),
    attribution_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_attributions_tenant_campaign
    ON attributions (tenant_id, campaign_id);
CREATE INDEX idx_attributions_attribution_at
    ON attributions (attribution_at);

ALTER TABLE attributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE attributions FORCE ROW LEVEL SECURITY;

-- Four policies (SELECT / INSERT / UPDATE / DELETE) mirror the campaigns
-- template (016_campaigns.sql). `app_current_tenant()` reads
-- `app.current_tenant` GUC set by `tenant_connection()` Python wrapper.
CREATE POLICY attributions_select ON attributions FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY attributions_insert ON attributions FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY attributions_update ON attributions FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY attributions_delete ON attributions FOR DELETE
    USING (tenant_id = app_current_tenant());


-- Cadence columns on campaigns. All nullable — pre-existing rows survive.
ALTER TABLE campaigns
    ADD COLUMN attribution_close_at  TIMESTAMPTZ NULL,
    ADD COLUMN attribution_closed_at TIMESTAMPTZ NULL,
    ADD COLUMN total_arrr_paise      BIGINT NULL
        CHECK (total_arrr_paise IS NULL OR total_arrr_paise >= 0);


-- Cadence column on tenants. Nullable; populated by VT-176's day-39 wiring
-- when a subscriber actually completes the paid-conversion event.
ALTER TABLE tenants
    ADD COLUMN paid_conversion_at TIMESTAMPTZ NULL;


COMMENT ON TABLE attributions IS
    'VT-175: per-payment attribution rows linking customer payments to '
    'the campaign that drove them. RLS via app_current_tenant() (CL-82 '
    'Standing). customer_id NULLABLE until VT-170 customers table.';
