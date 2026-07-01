-- 151_vt525_manager_tasks.sql — VT-525 (B2): the canonical Team-Manager TASK spine.
--
-- One row per owner-intent the manager takes on. This sits ABOVE the two existing guarded
-- effect pipelines (campaign-collapse AND the Gap-5 agent-draft path) as a supervisory /
-- evidence layer — it does NOT replace either. A task's steps (mig 152) point at whichever
-- pipeline actually fired via a polymorphic by-value evidence pointer; there is no unified
-- effects table and this migration does not invent one.
--
-- State machine: clarifying → planned → running → (waiting_owner|blocked) → verifying →
--   (completed|failed|cancelled). The CAS guard (store.set_task_status expected_from) forbids
--   a stale writer regressing a terminal state — the coordinator.py:_set_work_item_status
--   pattern (VT-374), reused verbatim. `version` bumps on every write for optimistic concurrency.
--
-- PII: `objective` / `acceptance_criteria` / `detail` are REDACTED by the store at write
-- (pii_redactor.redact, CL-390) — no raw owner/customer text at rest, mirroring tm_audit_log.
-- `source_message_ref` is a POINTER (run_id / message_sid), never the message body.
--
-- Tenant-scoped → RLS + FORCE in-migration + the operator-JWT SELECT policy (Fazal/VTR read of
-- redacted task state, mirrors mig 147) + swept in dsr_purge._PURGE_ORDER in the SAME PR
-- (VT-518/VT-524 house discipline).

CREATE TABLE manager_tasks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    objective           JSONB NOT NULL,                      -- REDACTED structured objective (never raw prose)
    acceptance_criteria JSONB NULL,                          -- REDACTED structured success criteria
    source_message_ref  TEXT NULL,                           -- pointer (run_id / message_sid), NOT the body
    assigned_function   TEXT NULL,                           -- owning specialist/lane (null until planned)
    policy_ref          TEXT NULL,                           -- owner-policy version applied (OC1 wires the check)
    status              TEXT NOT NULL DEFAULT 'clarifying' CHECK (status IN
        ('clarifying', 'planned', 'running', 'waiting_owner', 'blocked', 'verifying',
         'completed', 'failed', 'cancelled')),
    current_step_id     UUID NULL,                           -- soft pointer into manager_task_steps (no FK; steps FK back)
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{kind, ref}] accumulated terminal evidence
    idempotency_key     TEXT NULL,                           -- caller dedupe token (e.g. source message_sid) — redelivery-safe
    version             INT NOT NULL DEFAULT 1,              -- CAS / optimistic-concurrency counter
    stall_metadata      JSONB NULL,                          -- orphan-reaper marker (see orphan_reaper.reap_stalled_manager_tasks)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ NULL,
    CONSTRAINT manager_tasks_tenant_id_uniq UNIQUE (tenant_id, id)
);
-- True idempotency: a (tenant, idempotency_key) pair maps to exactly ONE task, ever — a
-- redelivered source event can never double-create. The store uses ON CONFLICT DO NOTHING +
-- read-back to return the existing task.
CREATE UNIQUE INDEX manager_tasks_idem ON manager_tasks (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX manager_tasks_tenant_status ON manager_tasks (tenant_id, status, created_at DESC);

ALTER TABLE manager_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY manager_tasks_select ON manager_tasks FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY manager_tasks_insert ON manager_tasks FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_tasks_update ON manager_tasks FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_tasks_delete ON manager_tasks FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Operator-JWT SELECT — Fazal/VTR read of REDACTED task state for the review loop. Mirrors
-- mig 147 tm_audit_operator_select verbatim. Rows carry no raw PII (redact-at-write), so the
-- operator sees ids + structured redacted facts only. Assignment-scoping (per-reviewer) is a
-- VT-533 (VTR contract) refinement layered later; Phase-1 reviewer = Fazal (operator_claim).
CREATE POLICY manager_tasks_operator_select ON manager_tasks
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
