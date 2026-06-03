-- 078_run_controls.sql — VT-300 VTR live run-control.
--
-- VT-293 recorded run-control INTENT in ops_audit (action='control_requested') but did not act on
-- a running workflow. VT-300 makes it real: a VTR pause/steer/override on a live run. This table is
-- the control queue the orchestrator graph consumes at node boundaries.
--
-- Semantics (Fazal + adversarial-review honest scoping, 2026-06-03):
--   - target = the REAL workflow webhook_pipeline_run → dispatch_brain (NOT the dead pipeline_run
--     smoke path). cancel_workflow can't suspend mid-step, so 'pause' = stop-at-next-node-boundary
--     (the graph handler re-arms an interrupt at the next node), honestly labeled — not mid-step.
--   - override IS VTR-issuable (Fazal ruled), but it's powerful → every control re-derives tenant
--     from run_id + re-checks operator_assignments SERVER-SIDE in the orchestrator (team-web auth is
--     fail-open at the enforcement leg) + audits to ops_audit. directive is PII-scrubbed at the
--     endpoint (a VTR types it while watching a customer convo — CL-390/CL-426).
--
-- Deny-all FORCE RLS: service-role only (the orchestrator endpoint + graph handler use the pool;
-- operator path has no tenant GUC). Append-of-intent + status state machine. CL-422 synthetic.
-- Migration 078 via the allocator (CL-424; reconciled forward past #248's in-flight 076/077).

CREATE TABLE IF NOT EXISTS public.run_controls (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL,                  -- the DBOS workflow id == pipeline_runs.id
    tenant_id     UUID NOT NULL,                  -- SERVER-derived from the run; never client-supplied
    control_type  TEXT NOT NULL CHECK (control_type IN ('pause', 'steer', 'override')),
    directive     TEXT NULL,                       -- PII-scrubbed steer/override note (no PII)
    requested_by  UUID NOT NULL,                  -- the operator (VTR/VTAdmin)
    status        TEXT NOT NULL DEFAULT 'requested'
                  CHECK (status IN ('requested', 'consumed', 'expired')),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at   TIMESTAMPTZ NULL
);

-- The graph handler's hot read: the oldest un-consumed control for a run.
CREATE INDEX IF NOT EXISTS idx_run_controls_pending
    ON public.run_controls (run_id, requested_at)
    WHERE status = 'requested';

ALTER TABLE public.run_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.run_controls FORCE ROW LEVEL SECURITY;
