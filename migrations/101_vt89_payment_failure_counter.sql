-- 101_vt89_payment_failure_counter.sql — VT-89: consecutive payment-failure counter.
--
-- Razorpay `payment.failed` webhooks transition a tenant to paid_at_risk only after
-- 3 CONSECUTIVE failures (structural threshold; raising/lowering is Type-2). The
-- counter lives on the subscription; any successful charge (payment.captured /
-- subscription.charged) resets it to 0. The increment + the reset both happen inside
-- the orchestrator razorpay-ingress dedup gate (razorpay_webhook_events event_id), so
-- a REDELIVERED payment.failed cannot double-increment.
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS consecutive_payment_failures INT NOT NULL DEFAULT 0;
