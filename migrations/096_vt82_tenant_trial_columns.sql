-- VT-82 — trial-period columns on tenants (signup mini-phase).
--
-- Set at signup (tenant creation): trial_started_at = now(); trial end is implicit
-- (trial_started_at + 14d). trial_extension_count tracks Day-39 extensions (the
-- evaluator owns the increments; signup just initializes to 0). Nullable +
-- default-0 so existing rows are unaffected.

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS trial_started_at      TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS trial_extension_count INTEGER NOT NULL DEFAULT 0;
