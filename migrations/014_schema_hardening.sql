-- 014_schema_hardening.sql — performance + integrity follow-ups from the
-- Layer 1 audit (CL-70 DC2/H3, CL-76 DC2 delta, Clau audit 2026-05-18).
--
-- Pillar 8: forward-only. This appends — 001 (tenants) and 006 (pipeline_steps)
-- are immutable after apply. The runner's schema_migrations tracking makes the
-- file idempotent at runner level (CREATE INDEX / ADD CONSTRAINT are not
-- re-run-safe via raw psql; the runner never re-applies an applied file).

-- DC2: tenants.whatsapp_number is the lookup key in twilio_ingress._lookup_tenant
-- — a full-table scan on every inbound webhook before this index. Not
-- tenant-scoped (cross-tenant lookup by design), so a plain B-tree is correct.
-- NOT made UNIQUE: per CL-76 DC2, _lookup_tenant's "most recent wins" semantics
-- tolerate duplicate numbers over time. UNIQUE would be a separate Decision.
CREATE INDEX tenants_whatsapp_number_idx ON tenants (whatsapp_number);

-- H3: step_index must be unique within a run so DBOS replay (or any future
-- replay) cannot duplicate observability rows. Also a structural assertion —
-- each step_index in a run records exactly one step.
--
-- Locking note for future-Fazal: ADD CONSTRAINT ... UNIQUE builds the index
-- under an ACCESS EXCLUSIVE lock on pipeline_steps. At Phase 1 scale (~10
-- design partners, low traffic) this is sub-second. At scale, switch to
-- CREATE UNIQUE INDEX CONCURRENTLY + ADD CONSTRAINT ... USING INDEX — out of
-- scope here.
ALTER TABLE pipeline_steps ADD CONSTRAINT pipeline_steps_run_step_unique
    UNIQUE (run_id, step_index);
