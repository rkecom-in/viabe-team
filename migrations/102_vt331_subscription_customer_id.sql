-- 102_vt331_subscription_customer_id.sql — VT-331: Razorpay customer ref on subscriptions.
--
-- /subscribe (orchestrator-side, service-role) binds BOTH razorpay_subscription_id
-- (mig 003, UNIQUE) and the Razorpay customer reference when it creates the
-- subscription. payment_method_last_four (display-only) is VT-91's concern.
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS razorpay_customer_id TEXT;

-- VT-331 review (HIGH): a DB-level backstop for the idempotency-before-vendor advisory
-- lock — at most ONE active subscription per tenant. A concurrent create that somehow
-- bypassed the lock violates this (its txn rolls back) instead of creating a duplicate.
-- Partial (status = 'active') so historical cancelled rows never block re-subscription.
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_active_per_tenant
    ON public.subscriptions (tenant_id)
    WHERE status = 'active';
