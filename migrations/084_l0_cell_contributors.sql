-- 084_l0_cell_contributors.sql — VT-225 (Option B, Fazal-locked).
--
-- Per-tenant k-anonymity admission for L0 fragments. VT-126's read gate uses
-- l0_fragments.observation_count >= 10 — a ROW counter, not a DISTINCT-TENANT
-- counter, so one tenant writing 10 observations of itself passes (poisoning).
-- This table tracks the DISTINCT tenants that contributed to each fragment;
-- admission = COUNT(distinct contributors) >= 10 (CL-28 k=10).
--
-- Option B + concurrency strategy (c) accept-race + idempotent insert: the PK
-- dedupes concurrent (fragment, tenant) inserts with NO locks (ON CONFLICT DO
-- NOTHING). The contributor INSERT chains in the SAME txn as the fragment UPSERT.
--
-- DSR-purge (CL-330/CL-416): ON DELETE CASCADE on tenant_id → purging a tenant
-- drops its contributor rows automatically and admission re-evaluates correctly.
--
-- Rule #14 / ground-truth correction: the VT-225 design doc DDL wrote
-- `fragment_id BIGINT` — but l0_fragments.id is UUID (mig 029). UUID it is.
-- Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE l0_cell_contributors (
    fragment_id          UUID NOT NULL REFERENCES l0_fragments (id) ON DELETE CASCADE,
    tenant_id            UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    first_contributed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fragment_id, tenant_id)
);

-- Index-served admission count (COUNT(*) WHERE fragment_id = ?).
CREATE INDEX idx_l0_cell_contributors_fragment
    ON l0_cell_contributors (fragment_id);

-- This is cross-tenant admission metadata (it reveals which tenants contributed
-- to a cell) — NOT tenant-readable data. Enable + FORCE RLS with NO policy so a
-- tenant-scoped (app_role) connection can never enumerate contributors; only the
-- service role (BYPASSRLS — the write/admission path) touches it. FK CASCADE is
-- unaffected by RLS.
ALTER TABLE l0_cell_contributors ENABLE ROW LEVEL SECURITY;
ALTER TABLE l0_cell_contributors FORCE ROW LEVEL SECURITY;
