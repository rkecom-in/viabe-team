-- 149_vt521_tm_audit_run_fk_drop.sql
-- VT-521 — drop the tm_audit_log.run_id → pipeline_runs FK (observability resilience).
--
-- WHY: tm_audit_log (VT-514, mig 147) is an append-only OBSERVABILITY spine. Plan principle #2:
-- "Observability is for detection and learning, NOT safety" — it must RECORD an event even when
-- the referenced pipeline_run is not (yet) persisted: an escalation seeded from a run marker, a
-- synthetic/test run, or an emit that races the pipeline_runs INSERT. The inline
--   run_id UUID NULL REFERENCES pipeline_runs(id)
-- (mig 147:41) makes a not-yet-persisted run_id FK-fail the audit INSERT
-- (constraint `tm_audit_log_run_id_fkey`); in the fail-closed (in-txn) emit mode that error
-- propagates and BREAKS THE PRIMARY OPERATION. record_escalation, the support-bot Fazal alert,
-- and the campaign complaint-freeze all regressed the moment VT-514 wired emit_tm_audit onto them.
--
-- trace_id (= str(run_id), mig 147:42) is ALREADY the deliberately FK-free cross-source join key,
-- so the run_id FK is BOTH redundant with trace_id AND inconsistent with it. Dropping it keeps the
-- run_id column + its partial index (tm_audit_run_idx) — joins/filters are unaffected — while the
-- audit spine never again blocks on referential integrity. This is NOT weakening a
-- correctness/eligibility/effect gate: tm_audit_log gates nothing; it observes.
--
-- DSR: VT-518 ordered tm_audit_log before pipeline_runs in dsr_purge._PURGE_ORDER because of this
-- FK (NO ACTION would block the pipeline_runs delete). With the FK gone that ordering is no longer
-- load-bearing, but it STAYS (correct children-before-parents hygiene, harmless).
-- Idempotent.

ALTER TABLE public.tm_audit_log
    DROP CONSTRAINT IF EXISTS tm_audit_log_run_id_fkey;

COMMENT ON COLUMN public.tm_audit_log.run_id IS
    'VT-521: soft pointer to pipeline_runs(id) — NO FK. Observability must record events for '
    'not-yet-persisted / synthetic runs. Join via the run_id index or trace_id (= str(run_id)).';
