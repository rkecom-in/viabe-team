-- 161_vt564_delivery_reconciliation.sql — VT-564: customer-send DELIVERY reconciliation.
--
-- A "sent" agent draft (agent_drafts.status='sent' + the agent_customer_contacts ledger row) records
-- transport ACCEPTANCE (Twilio returned a message_sid), NOT delivery. The async Twilio status
-- callback (delivered/read/failed/undelivered) is the delivery truth; nothing persisted it against
-- the customer-send ledger, and pre_filter routed only 'failed' (never 'undelivered') to a handler
-- that never reconciled back to the ledger — so a customer message could read 'sent' yet silently
-- never reach the customer (the VT-519 delivery-blindness, customer-send side; the owner side closed
-- in VT-524/534).
--
-- This adds a DELIVERY dimension to the per-customer contact ledger, distinct from the send-ACCEPTANCE
-- dimension (agent_drafts.status stays 'sent' — delivery failure never rewrites acceptance), plus a
-- message_sid index so the async callback can resolve sid → the contact row (WHERE tenant_id = %s AND
-- message_sid = %s — the existing indexes are keyed by customer/sent_at and serve no sid lookup).
--
-- Idempotent (ADD COLUMN / CREATE INDEX IF NOT EXISTS). No data rewrite — existing rows keep a NULL
-- delivery_status (delivery unknown, the honest default for pre-VT-564 sends). agent_customer_contacts
-- is already RLS + FORCE (mig 127); the reconciler writes service-role (get_pool) with an explicit
-- tenant_id, mirroring owner_notifications / tm_audit_log.

ALTER TABLE agent_customer_contacts
    ADD COLUMN IF NOT EXISTS delivery_status     TEXT NULL
        CHECK (delivery_status IN ('delivered', 'failed', 'undelivered')),
    ADD COLUMN IF NOT EXISTS delivery_updated_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS agent_customer_contacts_msgsid
    ON agent_customer_contacts (tenant_id, message_sid) WHERE message_sid IS NOT NULL;

COMMENT ON COLUMN agent_customer_contacts.delivery_status IS
    'VT-564: async Twilio delivery outcome (delivered/failed/undelivered); NULL = unknown/pending. '
    'Distinct from send acceptance — agent_drafts.status stays ''sent'' on a delivery failure.';
