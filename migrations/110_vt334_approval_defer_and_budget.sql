-- 110_vt334_approval_defer_and_budget.sql — VT-334: the "defer" approval decision + the
-- per-week messaging-budget index.
--
-- CL-428 sync: the `decision` CHECK must match the Python `Decision` Literal
-- (request_owner_approval.py) EXACTLY. VT-334 adds 'defer' to BOTH in this one PR.
-- The 052 inline CHECK is auto-named pending_approvals_decision_check; DROP + re-ADD it
-- (the pipeline_runs_status_check idiom) so the set is explicit + idempotent.
ALTER TABLE pending_approvals DROP CONSTRAINT IF EXISTS pending_approvals_decision_check;
ALTER TABLE pending_approvals ADD CONSTRAINT pending_approvals_decision_check
    CHECK (decision IS NULL OR decision IN (
        'approved', 'rejected', 'needs_changes', 'timeout', 'defer'));

-- A defer EXTENDS the window 48h (max 2), then is treated as rejected. defer_count tracks the
-- extensions; resolved_at stays NULL while pending. status stays 'pending' until exhaustion,
-- then decision='defer' + status='rejected' (the safe downstream behavior; audit truth in decision).
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS defer_count INT NOT NULL DEFAULT 0;

-- The per-week messaging budget counts campaign_send approvals per tenant in the last 7 days
-- (max 2). Index the count's predicate so the weekly fan-out guard isn't a seq-scan.
CREATE INDEX IF NOT EXISTS idx_pending_approvals_tenant_created
    ON pending_approvals (tenant_id, created_at);
