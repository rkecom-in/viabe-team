-- 064_imported_transactions_attribution_status.sql — VT-275 attribution bridge.
--
-- N3 (Cowork VT-275 ruling 2026-06-01): a TENTATIVE attribution (the bridge's
-- amount+date-only guess, made without a VPA) must be DISTINGUISHABLE from a
-- CONFIRMED one. customer_id alone would make a guess look attributed and could
-- leak into the clean ledger / reports / SR agent. So:
--   attribution_status ∈ (unattributed | tentative | confirmed):
--     - unattributed: no customer linked yet (the raw default).
--     - tentative:    the bridge's scored amount+date suggestion — NOT yet real;
--                     downstream MUST treat it as not-yet-attributed.
--     - confirmed:    a strong signal (VPA/phone resolved at import, or owner
--                     confirmation). ONLY status=confirmed ever writes a
--                     customer_ledger_entries row.
--   match_confidence: the bridge's score for a tentative link (NULL otherwise).
--
-- Backfill: rows attributed AT IMPORT (customer_id NOT NULL) were resolved via a
-- strong VPA/phone signal AND already promoted to the ledger (VT-276
-- record_imported_transactions) → 'confirmed'. customer_id NULL → 'unattributed'.
--
-- Migration number 064 via scripts/migration_id_allocate.py (CL-424). RLS already
-- on the table (062); column adds inherit it. CL-417 per-field columns, no JSONB.

ALTER TABLE public.imported_transactions
    ADD COLUMN IF NOT EXISTS attribution_status TEXT NOT NULL DEFAULT 'unattributed'
        CHECK (attribution_status IN ('unattributed', 'tentative', 'confirmed')),
    ADD COLUMN IF NOT EXISTS match_confidence REAL NULL
        CHECK (match_confidence IS NULL OR (match_confidence >= 0.0 AND match_confidence <= 1.0));

-- Backfill existing rows: attributed-at-import (strong signal, already in ledger)
-- → confirmed; everything else stays unattributed.
UPDATE public.imported_transactions
    SET attribution_status = 'confirmed'
    WHERE customer_id IS NOT NULL AND attribution_status = 'unattributed';

-- Bridge read path: unattributed rows awaiting a tentative suggestion.
CREATE INDEX IF NOT EXISTS idx_imported_transactions_tenant_status
    ON public.imported_transactions (tenant_id, attribution_status);
