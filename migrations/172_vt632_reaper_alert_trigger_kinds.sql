-- 172 (VT-632 confounder fix) — sync tenant_alerts_trigger_kind_check with the orphan_reaper's
-- emitted trigger_kinds.
--
-- The orphan reaper (orchestrator/orphan_reaper.py) dispatches three alert kinds —
-- 'orphaned_task', 'dead_letter_task', and 'silent_terminal' (VT-552) — but
-- tenant_alerts_trigger_kind_check (last set in mig 092) never included ANY of them. So every
-- reaper alert INSERT threw `psycopg.errors.CheckViolation` (fail-soft — dispatch_alert swallows
-- it and logs "VT-552 silent_terminal alert dispatch failed" — but the alert never persisted, and
-- the exception spammed dev logs during every reaper pass). This left the reaper's own
-- observability dark. Sync the CHECK with the set the code actually emits; purely additive
-- (widens the allowed set — no existing row can violate it).

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
        -- VT-632 (mig 172): the orphan_reaper's own alert kinds — emitted by orphan_reaper.py but
        -- never synced into this CHECK, so all three were rejected until now.
        'orphaned_task',
        'dead_letter_task',
        'silent_terminal'
    ));
