-- 127_vt369_agent_customer_contacts.sql — VT-369 Gap-5 PR-1: the per-customer agent-contact ledger.
--
-- One row per ACTUAL agent send to a customer. Drives: recontact suppression (30d), the 2-per-90d
-- ceiling, first-contact detection (the always-confirm floor), the L3 audit chain, and opt-out
-- attribution (a STOP ≤30d after a contact attributes to it). Tenant-scoped → RLS + FORCE +
-- dsr_purge._PURGE_ORDER.

CREATE TABLE agent_customer_contacts (
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    customer_id   UUID NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    agent         TEXT NOT NULL,
    draft_id      UUID NULL,
    batch_id      UUID NULL,
    template_name TEXT NOT NULL,
    autonomy_level TEXT NOT NULL DEFAULT 'L2' CHECK (autonomy_level IN ('L2', 'L3')),
    message_sid   TEXT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX agent_customer_contacts_customer ON agent_customer_contacts (tenant_id, customer_id, sent_at DESC);
CREATE INDEX agent_customer_contacts_recent ON agent_customer_contacts (tenant_id, sent_at DESC);

ALTER TABLE agent_customer_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_customer_contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_customer_contacts_select ON agent_customer_contacts FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY agent_customer_contacts_insert ON agent_customer_contacts FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_customer_contacts_update ON agent_customer_contacts FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_customer_contacts_delete ON agent_customer_contacts FOR DELETE
    USING (tenant_id = app_current_tenant());
