-- 108_vt332_consumed_subscribe_tokens.sql — single-use ledger for trial-end subscribe tokens.
-- Keyed on the token's `jti`: the orchestrator consumes it ATOMICALLY in razorpay-subscribe
-- (INSERT ... ON CONFLICT (jti) DO NOTHING → rowcount 0 means already-used → 403). A trial-end
-- deep-link is a PAYMENT link with a 7-day TTL; without this, the TTL is a replay window.
-- Workspace-wide (no tenant context needed for replay protection); tenant_id/plan_tier are
-- audit-only. NOT tenant-scoped → no DSR (the razorpay_webhook_events / consumed-ledger idiom).
CREATE TABLE consumed_subscribe_tokens (
    jti          TEXT PRIMARY KEY,
    consumed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id    UUID,    -- audit only (NOT used for access control)
    plan_tier    TEXT     -- audit only
);

-- Service-role-only (like razorpay_webhook_events / waitlist_signups): RLS forced deny-all so no
-- tenant-scoped connection can read or write the single-use ledger; the Supabase secret key /
-- Postgres superuser bypasses RLS — the sole intended access path (razorpay-subscribe consume).
ALTER TABLE consumed_subscribe_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE consumed_subscribe_tokens FORCE ROW LEVEL SECURITY;

CREATE POLICY consumed_subscribe_tokens_no_tenant_access ON consumed_subscribe_tokens
    FOR ALL
    USING (false)
    WITH CHECK (false);
