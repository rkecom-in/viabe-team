-- 025_pipeline_observability_normalize.sql — schema normalization to design-doc §2.1 (VT-187).
--
-- α-sequencing critical-path row per CL-417 (Fazal-Standing 2026-05-26).
-- Lands BEFORE VT-180 (write_step writer) so VT-180 + every downstream
-- consumer writes canonical per-field columns from day one. See
-- `.viabe/sprint/VT-187.md` + `.viabe/notifications/clau-briefing-2026-05-26-vt122-substrate-state.md` §3 Δ1.
--
-- CL-416 retention contract: this migration is column-additive +
-- back-fill-populating. ZERO row deletions. The Rule #15 canary
-- (Group B #6) snapshots pre/post count(*) on all 3 tables and FAILS on
-- any mismatch.
--
-- CL-417 guardrail: the existing JSONB envelopes (`trigger_payload`,
-- `terminal_state_metadata`, `input_envelope`, `output_envelope`)
-- remain as backwards-compatibility carriers. After this migration
-- lands, NO new envelope-only paths may be added — canonical per-field
-- columns are the canonical projection of well-known envelope fields.
--
-- CL-82 substrate: all 4 RLS policies per table are PRESERVED. This
-- migration does not touch `migrations/000b_rls_helpers.sql` or any
-- `CREATE POLICY` / `DROP POLICY` / `ALTER POLICY` statements.
-- `app_current_tenant()` continues to be the GUC bridge.
--
-- All ALTERs run in one transaction (default migration-runner posture).
-- ALTER TABLE ... RENAME COLUMN automatically updates dependent UNIQUE
-- constraint definitions + composite index column references in
-- PostgreSQL — no manual recreation needed for those.


-- ─── 1. pipeline_runs — canonical column adds + cost_paise rename + back-fill ───

ALTER TABLE pipeline_runs
    ADD COLUMN trigger_kind       TEXT,
    ADD COLUMN trigger_source_ref TEXT,
    ADD COLUMN final_outcome      TEXT,
    ADD COLUMN step_count         INT NOT NULL DEFAULT 0,
    ADD COLUMN error_summary      TEXT;

ALTER TABLE pipeline_runs RENAME COLUMN cost_paise TO total_cost_paise;

-- Back-fill from existing JSONB envelopes where data is recoverable.
UPDATE pipeline_runs SET
    trigger_kind       = COALESCE(trigger_payload->>'trigger_kind', run_type),
    trigger_source_ref = trigger_payload->>'source_ref',
    final_outcome      = terminal_state_metadata->>'final_outcome',
    error_summary      = terminal_state_metadata->>'error_summary';

-- Back-fill step_count via subquery over pipeline_steps.
UPDATE pipeline_runs r SET step_count = sub.cnt
  FROM (SELECT run_id, COUNT(*)::INT AS cnt FROM pipeline_steps GROUP BY run_id) sub
 WHERE r.id = sub.run_id;


-- ─── 2. pipeline_steps — canonical column adds + 3 renames + status back-fill ───

ALTER TABLE pipeline_steps
    ADD COLUMN step_name      TEXT,
    ADD COLUMN parent_step_id UUID NULL REFERENCES pipeline_steps (id),
    ADD COLUMN tool_calls     JSONB,
    ADD COLUMN status         TEXT,
    ADD COLUMN model_used     TEXT,
    ADD COLUMN tokens_input   INT,
    ADD COLUMN tokens_output  INT;

ALTER TABLE pipeline_steps RENAME COLUMN step_index     TO step_seq;
ALTER TABLE pipeline_steps RENAME COLUMN rationale      TO decision_rationale;
ALTER TABLE pipeline_steps RENAME COLUMN error_envelope TO error;

-- Back-fill status from error presence: existing rows with error → 'failed';
-- otherwise → 'completed'. Rows that were genuinely interrupted ('running')
-- can't be inferred from existing schema and would have been status NULL —
-- back-fill is conservative ('completed' is the dominant case for historical
-- rows; if status="running" matters later, VT-180's writer populates it
-- live going forward).
UPDATE pipeline_steps SET status = CASE
    WHEN error IS NULL THEN 'completed'
    ELSE 'failed'
END;

-- Apply NOT NULL + CHECK constraint AFTER back-fill so existing rows
-- pass the check.
ALTER TABLE pipeline_steps
    ALTER COLUMN status SET NOT NULL;

ALTER TABLE pipeline_steps
    ADD CONSTRAINT pipeline_steps_status_check
        CHECK (status IN ('running', 'completed', 'failed', 'skipped'));


-- ─── 3. phone_token_resolutions — column add + 2 renames ───

-- TODO(VT-170): add `REFERENCES customers(id)` once the customers table
-- ships. customer_id stays nullable until then. Per CL-417 review §Condition 1,
-- the brief's `REFERENCES tenants(id)` was incorrect (customer ≠ tenant);
-- ship as plain UUID NULL with no FK.
ALTER TABLE phone_token_resolutions
    ADD COLUMN customer_id UUID NULL;

ALTER TABLE phone_token_resolutions RENAME COLUMN token            TO phone_token;
ALTER TABLE phone_token_resolutions RENAME COLUMN last_resolved_at TO last_accessed_at;

-- Deltas from §2.1 spec (DELIBERATE UPGRADES, NOT regressions):
--   - `phone_number_encrypted` retained (encryption-at-rest is a privacy
--     improvement over §2.1's plaintext `phone_e164`).
--   - `resolved_count` retained (forensics counter additive to §2.1).
COMMENT ON COLUMN phone_token_resolutions.phone_number_encrypted IS
    'Encryption-at-rest (UPGRADE over §2.1 plaintext phone_e164); VT-187.';
COMMENT ON COLUMN phone_token_resolutions.resolved_count IS
    'Forensics counter — number of times this token has been resolved. '
    'Additive to §2.1 spec; VT-187.';
