-- 205_vt755_wedged_tenant_trigger_kind.sql — VT-755: admit the 'wedged_tenant' alert kind.
--
-- Additive widening of tenant_alerts_trigger_kind_check, paired with the same-PR Literal change in
-- alerts/triggers.py. This is the THIRD time this constraint has been widened (mig 172 for the
-- reaper's three kinds, mig 203 for fast_budget_exhausted), and the second time it is done with the
-- class-closing test in place — tests/orchestrator/test_vt746_trigger_kind_check_covers_literal.py
-- enumerates get_args(TriggerKind) and INSERTs every member, so a Literal addition without this
-- migration fails LOUDLY at test time instead of silently at 3am. That test is why this file exists
-- in the same change rather than being discovered missing later.
--
-- WHAT THE KIND MEANS. A tenant is WEDGED when a manager_task sits at 'waiting_owner' that nothing
-- can ever wake:
--
--   * _wake_waiting_workflow fires only from mark_approval_resolved, and there is no open approval;
--   * it also needs stall_metadata->>'wait_workflow_id', which is NULL on these rows;
--   * the retry ladder / orphan reaper deliberately EXCLUDES 'waiting_owner' (task_store.py:280) so
--     it can never burn an awaiting-approval task to dead_letter — correct for approvals, fatal here;
--   * pending_questions.correlate_reply only flips a row to 'answered' and sends no DBOS wake.
--
-- And because 'waiting_owner' is in TASK_ACTIVE, queue_promotion.promote_next_queued_task refuses to
-- advance anything while it sits there — and the promoter is only ever called from a TERMINAL task's
-- workflow tail, which this task will never reach. So every later objective for that tenant queues
-- behind it FOREVER. The tenant's Manager does not degrade, it ends, and nothing alerts today.
--
-- Severity is 'critical', unlike every other stall kind (all 'warning'): those degrade a tenant, this
-- one ends them, and no seam in the system recovers from it unaided.
--
-- Measured on deployed dev 2026-08-14: 4 of 7 'waiting_owner' tasks were un-wakeable and 1 tenant was
-- already wedged with a 'queued' task behind it.
--
-- Migration 205 via the allocator (CL-424). Additive ONLY — the existing CHECK is not narrowed, and
-- the negative test in the VT-746 file asserts the constraint still REFUSES an undeclared kind.

ALTER TABLE public.tenant_alerts
    DROP CONSTRAINT IF EXISTS tenant_alerts_trigger_kind_check;

ALTER TABLE public.tenant_alerts
    ADD CONSTRAINT tenant_alerts_trigger_kind_check CHECK (
        trigger_kind IN (
            'hard_limit',
            'escalation',
            'error_envelope',
            'cost_anomaly',
            'latency_anomaly',
            'privacy_audit_event',
            'volume_spike',
            'outbound_failure',
            'tenant_isolation_breach',
            'dsr_rate_anomaly',
            'pii_in_log',
            'reconstitution_sla_breach',
            'kg_drain_straggler',
            'orphaned_task',
            'silent_terminal',
            'dead_letter_task',
            'fast_budget_exhausted',
            'wedged_tenant'
        )
    );

COMMENT ON CONSTRAINT tenant_alerts_trigger_kind_check ON public.tenant_alerts IS
    'Must stay in lockstep with alerts.triggers.TriggerKind. Enforced by '
    'tests/orchestrator/test_vt746_trigger_kind_check_covers_literal.py, which INSERTs every member '
    'of the Literal against this CHECK — add a kind in Python and this constraint must be widened in '
    'the SAME change or that test fails. Widened by mig 172 (reaper kinds), 203 '
    '(fast_budget_exhausted), 205 (wedged_tenant).';
