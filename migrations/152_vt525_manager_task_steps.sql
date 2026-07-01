-- 152_vt525_manager_task_steps.sql — VT-525 (B2): ordered step plan under a manager_task.
--
-- The persisted, ORDERED plan of "what to do next" for a task (mig 151). This is the state
-- that B3's sequential multi-specialist loop reads/advances — it SUPERSEDES today's
-- one-spawn-per-turn model (each webhook turn spawned exactly one lane and terminated). A step
-- records what kind of work it is and, once executed, a POLYMORPHIC BY-VALUE pointer at the
-- pipeline that actually did it (`evidence_kind` + `evidence_ref`) — the campaign path and the
-- Gap-5 agent path don't share a key, so this is intentionally NOT a hard FK.
--
-- CAS: set_step_status(expected_from) forbids a stale writer regressing a terminal step
-- (coordinator VT-374 pattern). One step per (task_id, step_seq).
--
-- PII: `detail` (e.g. a manager-authored situation / desired_outcome for a specialist
-- dispatch — the B3 SpecialistHandoff framing) is REDACTED by the store at write.
--
-- Tenant-scoped → RLS + FORCE + operator SELECT + dsr_purge (steps BEFORE tasks, same PR).

CREATE TABLE manager_task_steps (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    task_id       UUID NOT NULL REFERENCES manager_tasks (id) ON DELETE CASCADE,
    step_seq      INT NOT NULL,                              -- order within the task plan (1-based)
    kind          TEXT NOT NULL CHECK (kind IN
        ('specialist_dispatch', 'effect', 'clarification', 'verification')),
    evidence_kind TEXT NULL CHECK (evidence_kind IN
        ('campaign_plan', 'agent_work_item', 'pipeline_run')),
    evidence_ref  TEXT NULL,                                 -- by-value id into the fired pipeline (no hard FK)
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'running', 'waiting', 'done', 'failed', 'skipped')),
    detail        JSONB NULL,                                -- REDACTED structured detail (situation/desired_outcome/return)
    version       INT NOT NULL DEFAULT 1,                    -- CAS / optimistic-concurrency counter
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT manager_task_steps_tenant_id_uniq UNIQUE (tenant_id, id)
);
CREATE UNIQUE INDEX manager_task_steps_seq ON manager_task_steps (task_id, step_seq);
CREATE INDEX manager_task_steps_tenant_status ON manager_task_steps (tenant_id, status);
-- Orphan-reaper predicate: "does this task have any non-terminal step?" scans by task_id+status.
CREATE INDEX manager_task_steps_task_status ON manager_task_steps (task_id, status);

ALTER TABLE manager_task_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_task_steps FORCE ROW LEVEL SECURITY;
CREATE POLICY manager_task_steps_select ON manager_task_steps FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY manager_task_steps_insert ON manager_task_steps FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_task_steps_update ON manager_task_steps FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_task_steps_delete ON manager_task_steps FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE POLICY manager_task_steps_operator_select ON manager_task_steps
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
