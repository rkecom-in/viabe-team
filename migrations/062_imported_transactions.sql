-- 062_imported_transactions.sql — VT-57/58/59 + VT-275 raw-import surface.
--
-- Two-surface transaction model (CC systemic-fork proposal 2026-06-01; Cowork
-- APPROVED + plan-reviewed same day). customer_ledger_entries (061) stays the
-- CLEAN, ATTRIBUTED customer ledger. THIS table is the RAW import surface:
--   - imports that resolve to a customer (phone/name/VPA) are ATTRIBUTED — they
--     write BOTH an imported_transactions row AND a customer_ledger_entries row.
--   - imports with NO resolvable customer (the common POS/UPI case) live ONLY
--     here, customer_id NULL, until match_transactions (VT-275) attributes them.
--
-- This REPLACES the separate F2 (tenant,source,bill_number) idempotency guard:
-- UNIQUE(tenant_id, source, provider_ref) folds it in (provider_ref = the
-- source's stable per-row id: bill_number / UPI txn ref / etc.).
--
-- Pillar 3: RLS in the same migration, FORCE (CL-82/88). CL-417: per-field
-- columns, NO JSONB envelope. CL-422: dev = synthetic only. Migration number 062
-- via scripts/migration_id_allocate.py (CL-424; never hand-picked).
--
-- N1 — REFUNDS/RETURNS (Cowork plan note): direction CHECK supports both
--   'credit' (customer pays owner) and 'debit' (refund/return reverses a sale).
--   Whether a given method RETAINS or drops debits is decided at the METHOD rows
--   (VT-57/58/59) — Cowork's lean is RETAIN refunds (real attribution signal).
--   The TABLE imposes no policy; it can hold both.
-- N2 — NO DOUBLE-COUNT ON PROMOTION (Cowork plan note): when VT-275 attributes a
--   row here and promotes it to customer_ledger_entries, the ledger's entry_key
--   UNIQUE(tenant_id, entry_key) idempotency (061) prevents a double-count on
--   re-promotion. Asserted in the VT-275 canary (promote → re-promote → 0 dupes).
--
-- customer_id is NULLABLE. The composite FK is MATCH SIMPLE (Postgres default):
-- a NULL customer_id row is NOT FK-checked (valid unattributed row); a non-NULL
-- customer_id is enforced same-tenant — cross-tenant linkage physically
-- impossible. customers.phone stays NOT NULL (no change without a separate
-- decision to Cowork).

CREATE TABLE IF NOT EXISTS public.imported_transactions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    -- NULL = unattributed (awaiting match_transactions / VT-275).
    customer_id  UUID NULL,
    -- import source; app-validated against the single-source VT-54 ACQUIRED_VIA enum.
    source       TEXT NOT NULL,
    -- the source's stable per-row id (bill_number / UPI txn ref / ...). Idempotency key.
    provider_ref TEXT NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),  -- magnitude; sign in direction
    txn_date     DATE NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
    notes        TEXT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FK target for any future child table (mirrors customers/ledger pattern).
    CONSTRAINT imported_transactions_tenant_id_uniq UNIQUE (tenant_id, id),
    -- same-tenant composite FK; NULL customer_id => not checked (MATCH SIMPLE).
    CONSTRAINT imported_transactions_customer_fk
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES public.customers (tenant_id, id) ON DELETE SET NULL,
    -- idempotency: re-import of the same source row = no-op.
    CONSTRAINT imported_transactions_idem UNIQUE (tenant_id, source, provider_ref)
);

-- Bridge read path (VT-275): unattributed rows for a tenant.
CREATE INDEX IF NOT EXISTS idx_imported_transactions_tenant_customer
    ON public.imported_transactions (tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_imported_transactions_tenant_date
    ON public.imported_transactions (tenant_id, txn_date);

ALTER TABLE public.imported_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.imported_transactions FORCE ROW LEVEL SECURITY;

CREATE POLICY imported_transactions_select ON public.imported_transactions
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY imported_transactions_insert ON public.imported_transactions
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY imported_transactions_update ON public.imported_transactions
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY imported_transactions_delete ON public.imported_transactions
    FOR DELETE USING (tenant_id = app_current_tenant());
