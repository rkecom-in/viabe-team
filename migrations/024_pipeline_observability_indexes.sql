-- 024_pipeline_observability_indexes.sql — composite-index hardening (VT-178).
--
-- VT-122.1 (VT-PipelineObservability substrate) — first sub-row.
-- See `.viabe/sprint/VT-178.md` + `.viabe/sprint/VT-122.md`.
--
-- The 3 tables (pipeline_runs, pipeline_steps, phone_token_resolutions)
-- already exist on main via migrations 005/006/007. Their existing
-- single-column `(tenant_id)` indexes are PRESERVED — query planner
-- picks the cheapest available; coexistence is safe.
--
-- This migration ADDS the composite indexes the design-doc §2.1
-- performance contract requires. Additive only; no column changes,
-- no RLS changes.
--
-- See VT-178 plan-ready §Q1 + Cowork review verdict: schema-name drift
-- between brief §2.1 and actual on-main columns is preserved here
-- (e.g. `step_index` not `step_seq`; `token` not `phone_token`).
-- Schema normalization to §2.1 spec is VT-187 (Cowork files post-merge).
--
-- Plain `CREATE INDEX` (not CONCURRENTLY) because the migration runner
-- wraps DDL in a transaction; CONCURRENTLY cannot run inside a
-- transaction. Phase-1 row counts are low enough that the short table
-- lock is acceptable. Future production-scale work can revisit via
-- `apply_migrations` enhancement.

-- pipeline_runs — Ops UI's "recent runs per tenant" query.
CREATE INDEX pipeline_runs_tenant_started_idx
    ON pipeline_runs (tenant_id, started_at DESC);

-- pipeline_steps — replay-tooling's "all steps in a run, in order" query.
CREATE INDEX pipeline_steps_run_step_idx
    ON pipeline_steps (run_id, step_index);

-- pipeline_steps — Ops UI's "recent steps per tenant" query.
CREATE INDEX pipeline_steps_tenant_started_idx
    ON pipeline_steps (tenant_id, started_at DESC);

-- phone_token_resolutions — tenant-scoped token lookup (existing
-- single-column tenant_id index is too broad for the token-prefixed
-- read path). Note: actual column is `token`, not `phone_token` per
-- brief §2.1.
CREATE INDEX phone_token_resolutions_tenant_token_idx
    ON phone_token_resolutions (tenant_id, token);
