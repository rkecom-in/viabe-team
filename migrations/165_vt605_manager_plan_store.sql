-- 165_vt605_manager_plan_store.sql — VT-605 (Loop Package 2): the plan spine becomes executable.
--
-- Additive-only extension of the existing manager_tasks (mig 151) + manager_task_steps (mig 152)
-- tables so a durable ``ManagerPlan``/``PlanStep`` (execution-plan §2) can be persisted, revised
-- (supersede-not-edit), and driven by a CAS-guarded store. No new table: a "plan" is the ordered
-- collection of ``manager_task_steps`` rows at a task's CURRENT ``plan_revision`` — a revision
-- appends NEW step rows at the next ``plan_revision`` and marks superseded the old-revision rows
-- still pending, never edits history (Package 2 "revisions never edit completed history").
--
--   manager_tasks.plan_revision           — the task's CURRENT plan revision (starts at 1; bumps
--                                            on every ``revise_plan``). Steps carry their OWN
--                                            plan_revision so a stale/old-revision step is never
--                                            confused with the live plan.
--   manager_tasks.terminal_outcome        — the FINAL, verified disposition (distinct from
--                                            ``status``'s state-machine terminal — this is the
--                                            business-meaningful outcome the owner is told about).
--                                            NULL until the task actually reaches a terminal status.
--   manager_tasks.owner_notification_status — has the owner been told the outcome yet (the VT-524
--                                            owner_notifications seam feeds this; NOT a duplicate
--                                            store — this is the manager_task's OWN view of it).
--   manager_tasks.status + 'queued'       — Package 2: "Additional objectives become queued" (ONE
--                                            active PLAN-STORE objective-bearing task per tenant;
--                                            a later objective while one is already active is
--                                            admitted as 'queued', not run, and not silently
--                                            dropped/blocked). Enforced at the APPLICATION level
--                                            (plan_store.create_plan, under the same tenants-row
--                                            FOR UPDATE lock task_store.create_task already uses)
--                                            — deliberately NOT a table-wide DB constraint: the
--                                            EXISTING legacy task_producer (VT-565) mints one
--                                            ephemeral task PER RUN and legitimately has multiple
--                                            concurrently-'running' tasks per tenant (e.g. an
--                                            overlapping scheduled cadence + a live turn) — a
--                                            blanket unique-active-task index would reject that
--                                            established, unrelated behavior (caught by the
--                                            pre-existing orphan_reaper test suite). The one-active
--                                            invariant is a NEW plan-store-level admission policy,
--                                            not a retroactive system-wide task-concurrency rule.
--
--   manager_task_steps.plan_revision      — which plan revision this step belongs to (mirrors the
--                                            task's plan_revision at the time the step was appended).
--   manager_task_steps.specialist         — WHICH of the three roster specialists a
--                                            'specialist_dispatch' step targets (VT-604: exactly
--                                            onboarding_conductor / integration_agent /
--                                            sales_recovery_agent — no other value is a valid
--                                            dispatch target). NULL for a non-dispatch step kind.
--   manager_task_steps.status + 'superseded' — a pending step from an OLD plan_revision, orphaned
--                                            by a ``revise_plan`` call. Distinct from 'skipped' (an
--                                            intentional in-plan skip) — 'superseded' means "this
--                                            step no longer exists in the current plan at all".
--   manager_task_steps.kind + 'advisory_tool' — the step dispatched a Manager-held advisory tool
--                                            (VT-604 ``agent/advisory_registry.py``) rather than a
--                                            roster specialist — 'specialist' stays NULL for these.
--   manager_task_steps.evidence_kind + 'pipeline_step' — a step's evidence may point at a single
--                                            ``pipeline_steps`` row (a granular in-run trace entry)
--                                            rather than a whole ``pipeline_run`` — finer-grained
--                                            than the existing 'pipeline_run' evidence kind.
--
-- No RLS change (existing tenant policies already cover every column on both tables). No new
-- child table, so no dsr_purge._PURGE_ORDER change. Both ALTERs are additive with safe DEFAULTs —
-- every pre-165 row is valid post-migration with no backfill needed.

-- ── manager_tasks ────────────────────────────────────────────────────────────
ALTER TABLE manager_tasks
    ADD COLUMN plan_revision             INT  NOT NULL DEFAULT 1,
    ADD COLUMN terminal_outcome          TEXT NULL,
    ADD COLUMN owner_notification_status TEXT NOT NULL DEFAULT 'not_required';

ALTER TABLE manager_tasks ADD CONSTRAINT manager_tasks_terminal_outcome_check CHECK (
    terminal_outcome IS NULL OR terminal_outcome IN (
        'completed_with_effect', 'completed_no_action', 'failed', 'escalated', 'cancelled'
    )
);
ALTER TABLE manager_tasks ADD CONSTRAINT manager_tasks_owner_notification_status_check CHECK (
    owner_notification_status IN ('not_required', 'pending', 'accepted', 'delivered', 'failed')
);

-- Extend the status CHECK to admit 'queued' (Package 2's per-tenant objective queue). Drop + re-add
-- (same idiom as mig 158's dead_letter addition) — no data rewrite, every existing status is still valid.
ALTER TABLE manager_tasks DROP CONSTRAINT manager_tasks_status_check;
ALTER TABLE manager_tasks ADD CONSTRAINT manager_tasks_status_check CHECK (status IN (
    'clarifying', 'planned', 'running', 'waiting_owner', 'blocked', 'verifying',
    'completed', 'failed', 'cancelled', 'dead_letter', 'queued'
));

-- NOTE: deliberately NO table-wide "one active task per tenant" unique index here. The one-active
-- admission rule is a NEW plan_store-level policy (application-enforced, under the tenants-row
-- FOR UPDATE lock create_plan/create_task already take) — see the manager_tasks.status comment
-- block above for why a blanket DB constraint would break the EXISTING legacy task_producer, which
-- legitimately mints multiple concurrently-'running' tasks per tenant (one per run).

COMMENT ON COLUMN manager_tasks.plan_revision IS
    'VT-605: current plan revision; steps carry their own matching plan_revision.';
COMMENT ON COLUMN manager_tasks.terminal_outcome IS
    'VT-605: the verified final disposition once the task reaches a terminal status.';
COMMENT ON COLUMN manager_tasks.owner_notification_status IS
    'VT-605: has the owner been told the terminal outcome yet.';

-- ── manager_task_steps ───────────────────────────────────────────────────────
ALTER TABLE manager_task_steps
    ADD COLUMN plan_revision INT  NOT NULL DEFAULT 1,
    ADD COLUMN specialist    TEXT NULL;

ALTER TABLE manager_task_steps ADD CONSTRAINT manager_task_steps_specialist_check CHECK (
    specialist IS NULL OR specialist IN (
        'onboarding_conductor', 'integration_agent', 'sales_recovery_agent'
    )
);

-- Extend kind / evidence_kind / status CHECKs (same drop + re-add idiom).
ALTER TABLE manager_task_steps DROP CONSTRAINT manager_task_steps_kind_check;
ALTER TABLE manager_task_steps ADD CONSTRAINT manager_task_steps_kind_check CHECK (kind IN (
    'specialist_dispatch', 'effect', 'clarification', 'verification', 'advisory_tool'
));

ALTER TABLE manager_task_steps DROP CONSTRAINT manager_task_steps_evidence_kind_check;
ALTER TABLE manager_task_steps ADD CONSTRAINT manager_task_steps_evidence_kind_check CHECK (
    evidence_kind IS NULL OR evidence_kind IN (
        'campaign_plan', 'agent_work_item', 'pipeline_run', 'pipeline_step'
    )
);

ALTER TABLE manager_task_steps DROP CONSTRAINT manager_task_steps_status_check;
ALTER TABLE manager_task_steps ADD CONSTRAINT manager_task_steps_status_check CHECK (status IN (
    'pending', 'running', 'waiting', 'done', 'failed', 'skipped', 'superseded'
));

-- The old (task_id, step_seq) unique index (mig 152) assumed ONE step per sequence position for
-- the task's whole lifetime — a revision that re-appends step_seq=1 under a NEW plan_revision would
-- collide with an old, now-superseded step_seq=1. Replace it with a (task_id, plan_revision,
-- step_seq) unique index: sequence numbers are unique WITHIN a revision, not across revisions —
-- exactly "revisions never edit history, they append" (Package 2).
DROP INDEX manager_task_steps_seq;
CREATE UNIQUE INDEX manager_task_steps_seq ON manager_task_steps (task_id, plan_revision, step_seq);

-- The claim/read path scans a task's CURRENT-revision steps in order; index it directly.
CREATE INDEX manager_task_steps_task_revision ON manager_task_steps (task_id, plan_revision, step_seq);

COMMENT ON COLUMN manager_task_steps.plan_revision IS
    'VT-605: the plan revision this step belongs to; unique with (task_id, step_seq).';
COMMENT ON COLUMN manager_task_steps.specialist IS
    'VT-605: the roster specialist a specialist_dispatch step targets (VT-604: exactly 3 values).';
