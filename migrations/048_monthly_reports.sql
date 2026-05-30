-- 048_monthly_reports.sql — VT-86 monthly impact report persistence.
--
-- One row per (tenant, reporting month). Tracks the rendered PDF's storage
-- path + the headline figures + email-delivery state, so the owner portal
-- (VT-9.7) can re-download and the monthly trigger can retry a failed send.
--
-- Migration number 048 claimed via scripts/migration_id_allocate.py (CL-424).
-- 047 is VT-240's attribution-method substrate (in-flight PR #158) — by-name
-- tracking in apply_migrations.py makes the 047 gap harmless if VT-86 merges
-- first.
--
-- Pillar 3: RLS lives in the same migration that creates the table.
-- Pillar 1: the report is built by deterministic SQL (no LLM); these rows are
--   written by the monthly-impact scheduled trigger body (VT-86 / D8).
-- CL-422: monthly_reports references tenant-identifying activity — dev holds
--   SYNTHETIC tenants only until prod-in-Mumbai (VT-231).
--
-- Column notes:
--   - arrr_paise: month ARRR (SUM of attributed paise for campaigns whose
--     attribution closed in the month). NULLABLE (a month may have none).
--   - fees_paid_paise / net_value_paise: DESCOPED for Phase-1 (subscriptions
--     only has a LIFETIME cumulative_fees_paid_paise, no per-month ledger).
--     Kept NULLABLE so a later writer can backfill once a fee ledger or a
--     confirmed flat-monthly model exists (VT-86 plan D3). The Phase-1 PDF
--     omits net-value rather than guess.
--   - email_sent_at: set when Resend accepts the send; NULL while unsent.
--   - email_failure_count: incremented on each failed send; the trigger
--     retries once (+1h) then alerts Fazal on the 2nd failure.

CREATE TABLE IF NOT EXISTS public.monthly_reports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    year_month          TEXT NOT NULL
                        CHECK (year_month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    pdf_storage_path    TEXT NULL,
    arrr_paise          BIGINT NULL
                        CHECK (arrr_paise IS NULL OR arrr_paise >= 0),
    fees_paid_paise     BIGINT NULL
                        CHECK (fees_paid_paise IS NULL OR fees_paid_paise >= 0),
    net_value_paise     BIGINT NULL,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    email_sent_at       TIMESTAMPTZ NULL,
    email_failure_count INT NOT NULL DEFAULT 0
                        CHECK (email_failure_count >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One report per tenant per month; the trigger UPSERTs on re-run.
    CONSTRAINT monthly_reports_tenant_month_uniq UNIQUE (tenant_id, year_month)
);

CREATE INDEX IF NOT EXISTS idx_monthly_reports_tenant_month
    ON public.monthly_reports (tenant_id, year_month);

ALTER TABLE public.monthly_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_reports FORCE ROW LEVEL SECURITY;

CREATE POLICY monthly_reports_select ON public.monthly_reports
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY monthly_reports_insert ON public.monthly_reports
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY monthly_reports_update ON public.monthly_reports
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY monthly_reports_delete ON public.monthly_reports
    FOR DELETE USING (tenant_id = app_current_tenant());

COMMENT ON TABLE public.monthly_reports IS
    'VT-86: one monthly impact report per (tenant, year_month). Stores the '
    'rendered PDF path + headline figures + Resend delivery state. RLS via '
    'app_current_tenant() (CL-82). fees/net-value NULLABLE — descoped Phase-1 '
    '(no per-month fee ledger).';
