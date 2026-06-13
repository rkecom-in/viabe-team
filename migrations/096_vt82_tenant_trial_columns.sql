-- VT-82 — trial-period columns on tenants (signup mini-phase).
--
-- Set at signup (tenant creation): trial_started_at = now(); trial end is implicit
-- (trial_started_at + trial_days from config/trial.yaml, currently 30d — VT-365: flat
-- 30-day trial, NO extensions). Nullable + default-0 so existing rows are unaffected.
-- NOTE (VT-390): trial_extension_count is now ORPHANED — the extension subsystem was
-- removed in 121_vt365_remove_refund_subsystem.sql and nothing increments it; the column
-- stays for backward compat. The original comment ("14d" / "Day-39 extensions") predated
-- the VT-365 policy and was corrected here.

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS trial_started_at      TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS trial_extension_count INTEGER NOT NULL DEFAULT 0;
