-- 061_customer_ledger_entries.sql — VT-273 transaction history.
--
-- Fazal pulled transaction history into Sprint 3 scope (2026-06-01): ingestion
-- methods PERSIST transactions (not identity-only). This is the table + the
-- forward-target VT-258 (query_customer_ledger real read) has been waiting on.
--
-- Pillar 3: RLS in the same migration (CL-82/88). CL-417: per-field columns, no
-- JSONB envelope. CL-422: dev = synthetic only. Migration number 061 via
-- scripts/migration_id_allocate.py.
--
-- IDEMPOTENCY (entry_key) + its KNOWN LIMITATION (Cowork N1, VT-273 review):
--   entry_key = sha256(tenant:customer:entry_date:amount_paise:entry_type).
--   UNIQUE(tenant_id, entry_key) + INSERT ON CONFLICT DO NOTHING makes
--   re-photographing the SAME ledger a no-op (no double-count). LIMITATION: two
--   GENUINELY-separate identical entries (same customer, same day, same amount,
--   same type) collapse into one — an UNDER-count. Accepted as the Phase-1
--   default: under-count << the re-ingest double-count it prevents. Future
--   disambiguation (a stable within-source ordinal) is VT-274 (Backlog) — do
--   NOT solve here.

CREATE TABLE IF NOT EXISTS public.customer_ledger_entries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL,
    customer_id       UUID NOT NULL,
    amount_paise      BIGINT NOT NULL CHECK (amount_paise >= 0),  -- magnitude; direction in entry_type
    entry_type        TEXT NOT NULL CHECK (entry_type IN ('sale', 'payment')),
    entry_date        DATE NOT NULL,
    notes             TEXT NULL,
    -- app-validated against the single-source VT-54 ACQUIRED_VIA enum.
    acquired_via      TEXT NOT NULL,
    -- the extraction confidence that committed this entry (VT-52 per-field).
    source_confidence REAL NOT NULL CHECK (source_confidence >= 0.0 AND source_confidence <= 1.0),
    entry_key         TEXT NOT NULL,        -- idempotency (see header)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- FK target for any future child table (mirrors customers/campaigns pattern).
    CONSTRAINT customer_ledger_entries_tenant_id_uniq UNIQUE (tenant_id, id),
    -- same-tenant composite FK: cross-tenant linkage physically impossible.
    CONSTRAINT customer_ledger_entries_customer_fk
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES public.customers (tenant_id, id) ON DELETE CASCADE,
    -- idempotency guarantee (re-ingest = no-op).
    CONSTRAINT customer_ledger_entries_idem UNIQUE (tenant_id, entry_key)
);

CREATE INDEX IF NOT EXISTS idx_customer_ledger_entries_tenant_customer
    ON public.customer_ledger_entries (tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_ledger_entries_tenant_date
    ON public.customer_ledger_entries (tenant_id, entry_date);

ALTER TABLE public.customer_ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customer_ledger_entries FORCE ROW LEVEL SECURITY;

CREATE POLICY customer_ledger_entries_select ON public.customer_ledger_entries
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY customer_ledger_entries_insert ON public.customer_ledger_entries
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customer_ledger_entries_update ON public.customer_ledger_entries
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY customer_ledger_entries_delete ON public.customer_ledger_entries
    FOR DELETE USING (tenant_id = app_current_tenant());
