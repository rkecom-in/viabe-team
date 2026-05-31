-- 060_customers_acquired_via.sql — VT-54 / VT-6.3 per-method source provenance.
--
-- VT-6 requires every ingested customer row to be queryable by acquired_via (the
-- ingestion method enum: paper_book | contacts | upi_phonepe | upi_gpay |
-- upi_paytm | kot_pos | cash_book | qr_opt_in | apify_zomato | apify_swiggy |
-- apify_magicpin | apify_gbp | owner_typed). A customer found via >1 method
-- accumulates tags (acquired_via_history) — the array is append-on-merge.
--
-- TEXT[] (not a PG enum): the enum is single-sourced in Python
-- (dedup_merge.ACQUIRED_VIA) + enforced app-side with a CI gate test that an
-- invalid tag is REJECTED. A PG enum would split the source of truth and make
-- adding a method a migration; the array keeps it a Python-only change (Pillar 8
-- config-driven). Additive (DEFAULT '{}'), backfills existing rows to empty.

ALTER TABLE public.customers
    ADD COLUMN IF NOT EXISTS acquired_via TEXT[] NOT NULL DEFAULT '{}';

-- Per-method observability (VT-6): "every ingested row queryable by acquired_via".
CREATE INDEX IF NOT EXISTS idx_customers_acquired_via
    ON public.customers USING GIN (acquired_via);
