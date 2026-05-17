-- 004_razorpay_webhook_events.sql — idempotency ledger for Razorpay webhooks.
-- Keyed on Razorpay's event.id so a redelivered webhook is processed once.
-- Workspace-wide (not tenant-scoped): there is no tenant context at webhook
-- receipt time.
CREATE TABLE razorpay_webhook_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT,
    payload      JSONB,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- Service-role-only: RLS is enabled + forced with a deny-all policy, so no
-- tenant-scoped connection can touch this table. The Supabase secret key /
-- Postgres superuser bypasses RLS — that is the sole intended access path.
ALTER TABLE razorpay_webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE razorpay_webhook_events FORCE ROW LEVEL SECURITY;

CREATE POLICY razorpay_webhook_events_no_tenant_access ON razorpay_webhook_events
    FOR ALL
    USING (false)
    WITH CHECK (false);
