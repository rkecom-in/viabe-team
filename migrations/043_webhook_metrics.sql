-- VT-226 — webhook_metrics append-only table.
--
-- Counter-style observability for inbound webhooks (Twilio, Razorpay,
-- Shopify, Drive Push). One row per event (sig_pass / sig_fail /
-- replay_rejected / rate_limit_rejected). NO PII; only event shape.
-- Source-IP retained for rate-limit telemetry; not joined to PII tables.

CREATE TABLE IF NOT EXISTS public.webhook_metrics (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,            -- 'twilio' | 'razorpay' | 'shopify' | 'google_drive'
    event TEXT NOT NULL,             -- 'sig_pass' | 'sig_fail' | 'replay_rejected' | 'rate_limit_rejected'
    message_sid TEXT,                -- nullable; only present for Twilio shape
    source_ip TEXT NOT NULL,
    response_status INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_metrics_source_created
    ON public.webhook_metrics (source, created_at DESC);
