-- 167_vt606_shadow_task_status.sql — VT-606 round-3 adversarial-review fix: add 'shadow' to
-- manager_tasks.status.
--
-- Finding: triage_seam.py's shadow-mode new_task creation called plan_store.create_plan exactly
-- like a real turn — the resulting task landed in 'planned'/'queued', a member of
-- task_store.TASK_ACTIVE, and NOTHING ever drives/claims/settles it (shadow never dispatches). It
-- occupied the tenant's one-active-task admission slot FOREVER; the stalled-task reaper doesn't
-- reap it either (it was never 'running'). A real new_task right behind a shadow-mode turn would
-- be wrongly admitted as 'queued' instead of 'planned'.
--
-- Fix: shadow-mode plans persist status='shadow' — excluded from task_store.TASK_ACTIVE (so they
-- never occupy the admission slot, are never claimed, and are never a queue-promotion candidate),
-- but the PLAN CONTENT stays queryable (not audit-only) — that's what the 50-conversation shadow
-- compare needs to review for divergence. Additive: widen the existing CHECK, drop + re-add (same
-- idiom as mig 165's own 'queued' addition) — no data rewrite, every existing row keeps a valid
-- status.

ALTER TABLE manager_tasks DROP CONSTRAINT manager_tasks_status_check;
ALTER TABLE manager_tasks ADD CONSTRAINT manager_tasks_status_check CHECK (status IN (
    'clarifying', 'planned', 'running', 'waiting_owner', 'blocked', 'verifying',
    'completed', 'failed', 'cancelled', 'dead_letter', 'queued', 'shadow'
));
