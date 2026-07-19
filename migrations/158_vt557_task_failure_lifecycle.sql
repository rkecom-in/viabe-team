-- 158_vt557_task_failure_lifecycle.sql — VT-557 (B6): retry-backoff + dead-letter for manager_tasks.
--
-- The B6 reliability slice: a manager_task that repeatedly stalls (a process died between planning
-- and stepping) currently parks at 'blocked' forever, needing a human. VT-557 gives it a BOUNDED,
-- deterministically-backed-off retry ladder (reuses backoff.compute_delay) and a real DEAD_LETTER
-- terminal on exhaustion — plus an operator redrive to re-dispatch a dead-lettered task.
--
--   attempt        — how many times the reaper has caught this task stalled (0 = never).
--   max_attempts   — retry budget (default 5, matches backoff.MAX_ATTEMPTS). attempt >= max → dead_letter.
--   next_retry_at  — the backoff gate: the reaper skips a task until this elapses (deterministic
--                    exponential + jitter, backoff.compute_delay). NULL = eligible now.
--   status 'dead_letter' — the retry-exhausted terminal (operator-redrivable, NOT auto-retried again).

ALTER TABLE manager_tasks
    ADD COLUMN attempt       INT NOT NULL DEFAULT 0,
    ADD COLUMN max_attempts  INT NOT NULL DEFAULT 5,
    ADD COLUMN next_retry_at TIMESTAMPTZ NULL;

-- Extend the status CHECK to admit the dead_letter terminal (drop + re-add; the column keeps its
-- existing default 'clarifying'). No data rewrite — all existing rows hold a still-valid status.
ALTER TABLE manager_tasks DROP CONSTRAINT manager_tasks_status_check;
ALTER TABLE manager_tasks ADD CONSTRAINT manager_tasks_status_check CHECK (status IN
    ('clarifying', 'planned', 'running', 'waiting_owner', 'blocked', 'verifying',
     'completed', 'failed', 'cancelled', 'dead_letter'));

-- The reaper's retry-gate scan: stalled + next_retry_at elapsed. Partial on the states it sweeps.
CREATE INDEX manager_tasks_retry_gate ON manager_tasks (next_retry_at)
    WHERE status IN ('planned', 'running', 'verifying');

COMMENT ON COLUMN manager_tasks.attempt IS
    'VT-557: reaper stall count; attempt >= max_attempts → dead_letter.';
COMMENT ON COLUMN manager_tasks.next_retry_at IS
    'VT-557: backoff gate (compute_delay); reaper skips the task until this elapses.';
