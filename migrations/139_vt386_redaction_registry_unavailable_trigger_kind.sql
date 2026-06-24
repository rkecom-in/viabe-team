-- 139_vt386_redaction_registry_unavailable_trigger_kind.sql — VT-386 redaction-registry outage alert kind.
--
-- VT-386 fail-soft-split outage handling (Fazal-ruled): when the customer-name
-- redaction registry is unavailable on a write path, the observability writers
-- (a) still write the row, (b) strip the KNOWN name-key fields (customer_name /
-- owner_name) to `<name:registry_down>` WITHOUT the registry, and (c) fire a new
-- CRITICAL `redaction_registry_unavailable` alert. The free-text-name residual is
-- now an ALERTED pattern gap, not a silent leak.
--
-- Per CL-428 the DB CHECK on tenant_alerts.trigger_kind MUST stay synced to the
-- TriggerKind Literal in alerts/triggers.py — so adding the kind = this
-- CHECK-extending migration in the SAME PR as the Literal edit. Re-creates the
-- constraint to the full code set (13 existing + redaction_registry_unavailable
-- = 14) rather than encode a subset.
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
        -- VT-307 KG-drain straggler (mig 092)
        'kg_drain_straggler',
        -- VT-386 redaction name-registry outage (fail-soft split escalation)
        'redaction_registry_unavailable'
    ));
