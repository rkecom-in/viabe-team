-- 092_vt307_kg_drain_straggler_trigger_kind.sql — VT-307 KG-drain straggler alert kind.
--
-- The nightly KG-events outbox-drain sweep (VT-307) raises a `kg_drain_straggler`
-- alert (warning) when drain_kg_events reports `failed > 0` for a tenant — an
-- outbox event the immediate (VT-65) + nightly drain both failed to project
-- (a reliability backstop signal). Per CL-428 the DB CHECK on
-- tenant_alerts.trigger_kind MUST stay synced to the TriggerKind Literal in
-- alerts/triggers.py — so adding the kind = this CHECK-extending migration in the
-- SAME PR as the Literal edit. Re-creates the constraint to the full code set
-- (12 existing + kg_drain_straggler = 13) rather than encode a subset.
-- Claimed via scripts/migration_id_allocate.py (CL-424).

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
        -- VT-79 breach detectors (synced into the CHECK by mig 089)
        'tenant_isolation_breach',
        'dsr_rate_anomaly',
        'pii_in_log',
        -- VT-76 reconstitution SLA (mig 089)
        'reconstitution_sla_breach',
        -- VT-307 KG-drain straggler
        'kg_drain_straggler'
    ));
