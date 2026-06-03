-- 089_vt76_reconstitution.sql — VT-76 opt-out 7-day reconstitution (the closing moat row).
--
-- Reconstitution = the opt-out RIGHT: 7 days after a customer opts out, the
-- agent de-LINKS that customer from its L2 episodic footprint
-- (episodic_events.referenced_entity_id → all-zeros sentinel) while KEEPING the
-- event row. Distinct from CL-416 DSR-purge (full deletion, a different right):
--   - de-linking stops re-identification (the opt-out right);
--   - keeping the row preserves k-anon aggregate integrity — the cohort counts
--     L3 depends on stay intact (deleting would corrupt them). Cowork ruling
--     20260604T033000Z Call-1: sentinel-null over delete. CONFIRMED.
--
-- Two columns drive the daily sweep (opt_out_status already exists, mig 045):
--   - opt_out_at                 when the opt-out landed (the 7-day clock start)
--   - reconstitution_completed_at when the sweep finished de-linking (NULL = pending)
--
-- The receive side (inbound STOP classifier that SETS opt_out_at) is VT-318 —
-- gate-live on the customer-inbound path (WABA). This row ships the MECHANISM
-- (sweep + SLA + columns); it is correct + canaried on synthetic
-- customer-referencing episodic rows and a no-op on real data until VT-312's
-- detectors emit customer-referencing events (VT-312 Blocked on Fazal thresholds).
--
-- Claimed via scripts/migration_id_allocate.py (CL-424). CL-422: dev synthetic-only.

-- ===================== 1. reconstitution columns =====================

ALTER TABLE public.customers
    ADD COLUMN IF NOT EXISTS opt_out_at                  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reconstitution_completed_at TIMESTAMPTZ;

-- The sweep's eligibility scan: opted-out, clock elapsed, not-yet-reconstituted.
-- Partial index keeps the daily workspace-wide scan cheap as the table grows.
CREATE INDEX IF NOT EXISTS idx_customers_reconstitution_pending
    ON public.customers (opt_out_at)
    WHERE opt_out_status = 'opted_out' AND reconstitution_completed_at IS NULL;

-- ===================== 2. SLA-breach trigger kind ====================
-- The 7-day reconstitution SLA fires `reconstitution_sla_breach` (critical) on
-- the existing VT-202 alerts/triggers path (Cowork ruling Call-2: distinct kind,
-- reuse the path — not a new alert system). tenant_alerts.trigger_kind is a
-- CHECK-constrained TEXT (mig 037); the constraint must list the new kind or the
-- _persist_alert INSERT rejects it.
--
-- LATENT-BUG REPAIR (flagged to Cowork): mig 037's CHECK still lists only the 8
-- original kinds. VT-79 added `tenant_isolation_breach` / `dsr_rate_anomaly` /
-- `pii_in_log` to the Python TriggerKind Literal (alerts/triggers.py) but NO
-- migration ever extended the DB CHECK — so those detectors would violate the
-- constraint on their first real dispatch (masked today only because they are
-- gate-live / fire on empty data). Re-creating the CHECK to add
-- `reconstitution_sla_breach` while knowingly omitting the 3 code-referenced
-- kinds would re-encode a known-wrong constraint, so this syncs the CHECK to the
-- code's full TriggerKind set (8 original + VT-79's 3 + VT-76's 1 = 12). The
-- VT-79 portion is a repair, not VT-76 scope — see the task-result roster note.

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
        -- VT-79 breach detectors (constraint was never extended — repaired here)
        'tenant_isolation_breach',
        'dsr_rate_anomaly',
        'pii_in_log',
        -- VT-76 reconstitution SLA
        'reconstitution_sla_breach'
    ));
