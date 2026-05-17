-- 012_twilio_inbound_events.sql — inbound Twilio message idempotency ledger
-- (VT-3.3a). Belt-and-suspenders alongside DBOS workflow_id idempotency.
CREATE TABLE twilio_inbound_events (
    message_sid TEXT PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants (id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX twilio_inbound_events_tenant_idx ON twilio_inbound_events (tenant_id);

-- Pillar 3: tenant-scoped RLS, in the same migration that creates the table.
ALTER TABLE twilio_inbound_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE twilio_inbound_events FORCE ROW LEVEL SECURITY;

CREATE POLICY twilio_inbound_events_select ON twilio_inbound_events FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY twilio_inbound_events_insert ON twilio_inbound_events FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY twilio_inbound_events_update ON twilio_inbound_events FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY twilio_inbound_events_delete ON twilio_inbound_events FOR DELETE
    USING (tenant_id = app_current_tenant());
