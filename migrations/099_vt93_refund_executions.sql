-- 099_vt93_refund_executions.sql — VT-93: refund-execution ledger + 30-day graceful exit.
--
-- The refund-conversation engine (VT-85) / day-39 auto-path calls
-- billing/refund_executor.execute_refund(tenant_id, refund_reason). This table is
-- the durable state machine + idempotency ledger for that execution:
--   status: pending -> refunding -> (partial_failed | pending_subscription_cancel) -> completed
-- PK (tenant_id, refund_reason) is the idempotency key — one execution per
-- (tenant, reason). INSERT ... ON CONFLICT DO NOTHING + pg_advisory_xact_lock +
-- SELECT ... FOR UPDATE serialize concurrent calls (no double-refund).
--
-- IMMUTABILITY (financial): once status='completed' the row is frozen — a trigger
-- blocks UPDATE/DELETE/TRUNCATE for EVERY role incl. the BYPASSRLS service role
-- (mig 079 precedent), so a completed refund record cannot be silently mutated.
-- The DSR purge is the SOLE legitimate deleter of a completed row: it sets
-- `SET LOCAL orchestrator.dsr_purge_in_progress = 'on'` and the trigger exempts
-- that session, allowing the DPDP right-to-erasure hard-delete. The durable audit
-- of the refund survives in privacy_audit_log (mig 079 immutable hash-chain,
-- DSR-exempt), so hard-deleting this row loses no compliance substrate.
--
-- DSR: tenant subject billing data -> in dsr_purge._PURGE_ORDER (hard-delete).
-- Leaf table: FK to tenants(id) only (day39_evaluation_id is an id-copy, NOT a FK,
-- to keep the row immutable + the purge order unconstrained).

-- 30-day graceful-exit window anchor. Set atomically with phase='refunded' by the
-- refund executor (avoids a NULL window). portal_access_allowed() reads it.
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS public.refund_executions (
    tenant_id            UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    refund_reason        TEXT NOT NULL
                         CHECK (refund_reason IN ('day39_eligibility', 'manual_request')),
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN (
                             'pending', 'refunding', 'partial_failed',
                             'pending_subscription_cancel', 'completed')),
    total_refund_paise   BIGINT NOT NULL DEFAULT 0 CHECK (total_refund_paise >= 0),
    partial_refund_paise BIGINT NOT NULL DEFAULT 0 CHECK (partial_refund_paise >= 0),
    refund_responses     JSONB NOT NULL DEFAULT '[]'::jsonb,
    day39_evaluation_id  UUID,            -- id-copy for decision->execution audit (NOT a FK)
    notification_pending BOOLEAN NOT NULL DEFAULT false,  -- template SID null (NEEDS-FAZAL) on a completed refund
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, refund_reason)
);

-- Sweep index: pending_subscription_cancel retry + in-flight recovery scans.
CREATE INDEX IF NOT EXISTS idx_refund_executions_active
    ON public.refund_executions (status, updated_at)
    WHERE status IN ('pending', 'refunding', 'pending_subscription_cancel');

-- Pillar 3: tenant-scoped RLS, same migration (mirror day39_evaluations / mig 098).
ALTER TABLE public.refund_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.refund_executions FORCE ROW LEVEL SECURITY;

CREATE POLICY refund_executions_select ON public.refund_executions
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY refund_executions_insert ON public.refund_executions
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY refund_executions_update ON public.refund_executions
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY refund_executions_delete ON public.refund_executions
    FOR DELETE USING (tenant_id = app_current_tenant());

-- Immutability on completed rows — TRIGGER, not RLS alone (RLS is bypassed by the
-- service-role conn; the trigger fires for every role). Intermediate transitions
-- (OLD.status != 'completed') are allowed; the DSR purge session is exempt so the
-- right-to-erasure hard-delete succeeds.
CREATE OR REPLACE FUNCTION refund_executions_immutable_row()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'completed'
       AND coalesce(current_setting('orchestrator.dsr_purge_in_progress', true), '') <> 'on' THEN
        RAISE EXCEPTION
            'refund_executions is immutable once status=completed (VT-93 financial ledger); % blocked',
            TG_OP;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER refund_executions_immutable_row
    BEFORE UPDATE OR DELETE ON public.refund_executions
    FOR EACH ROW EXECUTE FUNCTION refund_executions_immutable_row();

-- No legitimate TRUNCATE path (DSR uses row DELETE under the session flag).
CREATE OR REPLACE FUNCTION refund_executions_no_truncate()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'refund_executions: TRUNCATE blocked (VT-93 financial ledger)';
END;
$$;

CREATE TRIGGER refund_executions_no_truncate
    BEFORE TRUNCATE ON public.refund_executions
    FOR EACH STATEMENT EXECUTE FUNCTION refund_executions_no_truncate();

-- New refund audit event types on the immutable privacy_audit_log hash-chain.
-- CL-428 / mig 079+080 discipline: extend the event_type CHECK as new types are
-- actually written (refund_executor.py writes these via log_privacy_event). Carry
-- forward the existing 5 (079 seeded 3; 080 added the 2 dsr_export types).
ALTER TABLE privacy_audit_log DROP CONSTRAINT privacy_audit_log_event_type_chk;
ALTER TABLE privacy_audit_log ADD CONSTRAINT privacy_audit_log_event_type_chk
    CHECK (event_type IN (
        'phone_token_resolved',
        'subject_data_purged',
        'subject_data_purged_table',
        'dsr_export_requested',
        'dsr_export_completed',
        'refund_executed',
        'refund_partial_failed'
    ));
