-- 203 (VT-746) — admit 'fast_budget_exhausted' into tenant_alerts_trigger_kind_check.
--
-- THE DEFECT
-- ----------
-- `alerts/triggers.py` declares `fast_budget_exhausted` in the `TriggerKind` Literal and gives it a
-- severity, but it was never added to `tenant_alerts_trigger_kind_check` (last widened by mig 172).
-- A real INSERT of that kind against a fully migrated database is REJECTED by the CHECK — proven by
-- execution, not by reading the code.
--
-- `llm/fast_budget.py` dispatches the trigger inside a fail-soft `except`, so the CheckViolation is
-- swallowed. The row never lands and nobody is ever paged. VT-735 exists to stop a tenant silently
-- burning the Fast tier: enforcement works, and the part that TELLS ANYONE has never once fired.
--
-- THIS IS THE SECOND TIME
-- -----------------------
-- Migration 172 was written for byte-for-byte this class — three reaper trigger kinds declared in
-- Python that the database refused, leaving the reaper's own observability dark. It recurred because
-- NOTHING TIES THE LITERAL TO THE CHECK. So the real deliverable of VT-746 is not this file: it is
-- `tests/orchestrator/test_vt746_trigger_kind_check_covers_literal.py`, which enumerates every
-- member of the `TriggerKind` Literal and INSERTs it against the live CHECK. The next kind added in
-- Python now fails loudly in CI instead of silently at 3am.
--
-- Purely additive: widening the allowed set cannot invalidate an existing row.

ALTER TABLE public.tenant_alerts
    DROP CONSTRAINT IF EXISTS tenant_alerts_trigger_kind_check;

ALTER TABLE public.tenant_alerts
    ADD CONSTRAINT tenant_alerts_trigger_kind_check CHECK (trigger_kind IN (
        -- mig 037 originals
        'hard_limit',
        'escalation',
        'error_envelope',
        'cost_anomaly',
        'latency_anomaly',
        'privacy_audit_event',
        'volume_spike',
        'outbound_failure',
        -- VT-79 breach detectors (mig 089)
        'tenant_isolation_breach',
        'dsr_rate_anomaly',
        'pii_in_log',
        -- VT-76 reconstitution SLA (mig 089)
        'reconstitution_sla_breach',
        -- VT-307 KG-drain straggler (mig 092)
        'kg_drain_straggler',
        -- VT-632 orphan_reaper kinds (mig 172)
        'orphaned_task',
        'dead_letter_task',
        'silent_terminal',
        -- VT-746 (mig 203): VT-735's Fast-tier budget tripwire. Declared in the TriggerKind Literal
        -- since VT-735 and rejected by this CHECK ever since, so the budget could exhaust in silence.
        'fast_budget_exhausted'
    ));

COMMENT ON CONSTRAINT tenant_alerts_trigger_kind_check ON public.tenant_alerts IS
    'VT-746: must stay in sync with the TriggerKind Literal in alerts/triggers.py. A kind declared '
    'in Python and absent here is an alert that CANNOT BE WRITTEN — the dispatch is fail-soft, so it '
    'fails silently and nobody is paged. Enforced by '
    'tests/orchestrator/test_vt746_trigger_kind_check_covers_literal.py, which INSERTs every Literal '
    'member against this constraint. Add a kind to the Literal => widen this CHECK in the same PR.';
