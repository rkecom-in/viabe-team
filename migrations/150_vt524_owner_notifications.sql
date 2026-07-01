-- 150_vt524_owner_notifications.sql
-- VT-524 (B1) — owner-notification delivery ledger. Closes the VT-519 delivery-blindness.
--
-- A Twilio transport SID at send time proves ACCEPTANCE, not delivery; the async status callback
-- (delivered / failed / undelivered) is what proves the owner was actually reached. Today the
-- outbound owner send captures a message_sid (SendResult) but NOTHING persists the async delivery
-- result — a welcome could be 'accepted' yet silently never delivered (exactly the 63049 case).
-- This table records one row per owner-facing notification send, keyed by the outbound message_sid,
-- and is UPDATEd by the async status callback in the runner.
--
-- Plan B1 states:
--   owner_notification_status = not_required | pending | accepted | delivered | failed
--   communication_status      = delivered | failed_incident_open   (owner-facing terminals only)
-- A transport SID = 'accepted'; 'delivered'/'failed' arrive later by callback. not_required carries
-- a deterministic reason (internal runs), never a silent default.
--
-- Privacy lifecycle IN THIS MIGRATION (plan mandate + CL-416 + the VT-518 lesson): tenant RLS +
-- FORCE ROW LEVEL SECURITY; retention = lifetime-of-relationship, DSR-purge the sole deletion path
-- (owner_notifications is registered in dsr_purge._PURGE_ORDER in the SAME PR). NO raw phone/body/name
-- columns — only the opaque Twilio message_sid, the template_name, and status. run_id is a SOFT
-- pointer with NO FK (VT-521 lesson: a notification/observability ledger must never gate on
-- referential integrity to a possibly-unpersisted run). Writes go through the RLS-bypassing service
-- pool (get_pool) with an explicit tenant_id, mirroring tm_audit_log / escalations.
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.owner_notifications (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES public.tenants(id),
    run_id                      UUID NULL,               -- soft pointer, NO FK (VT-521)
    template_name               TEXT NULL,
    message_sid                 TEXT NULL,               -- outbound Twilio SID; join key for the async callback
    owner_notification_status   TEXT NOT NULL DEFAULT 'pending'
        CHECK (owner_notification_status IN
               ('not_required', 'pending', 'accepted', 'delivered', 'failed')),
    communication_status        TEXT NULL
        CHECK (communication_status IN ('delivered', 'failed_incident_open')),
    not_required_reason         TEXT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at                 TIMESTAMPTZ NULL,
    delivered_at                TIMESTAMPTZ NULL,
    failed_at                   TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS owner_notifications_msgsid_idx
    ON public.owner_notifications (message_sid) WHERE message_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS owner_notifications_tenant_idx
    ON public.owner_notifications (tenant_id);

ALTER TABLE public.owner_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.owner_notifications FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_notifications_select ON public.owner_notifications FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY owner_notifications_insert ON public.owner_notifications FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_notifications_update ON public.owner_notifications FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_notifications_delete ON public.owner_notifications FOR DELETE
    USING (tenant_id = app_current_tenant());
