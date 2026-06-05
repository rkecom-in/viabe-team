-- 103_vt91_payment_method_last_four.sql — VT-91: display-only masked card last-4.
--
-- PCI-safe: the last 4 digits only (per PCI-DSS SAQ-A this is not sensitive cardholder
-- data). NEVER store PAN / CVV / expiry. The column lands here (VT-91); POPULATION is
-- deferred to VT-330 — the VT-89 webhook will extract ONLY card.last4 (a surgical
-- whitelist on the otherwise routing-only redacted payload; CL-390).
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS payment_method_last_four TEXT;
