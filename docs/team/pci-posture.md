# Viabe Team — PCI-DSS posture (VT-91)

**Merchant classification: SAQ-A** (Self-Assessment Questionnaire A).

Viabe Team fully outsources cardholder data to **Razorpay**. Card details (PAN, CVV,
expiry) are entered ONLY on Razorpay's hosted Checkout form / iframe — they never touch
Viabe infrastructure, are never transmitted through our servers, and are never stored or
logged by us. This qualifies Viabe Team as a SAQ-A merchant (card data entry + processing
fully delegated to a PCI-DSS Level 1 service provider).

## What we store (PCI-safe)
- `subscriptions.razorpay_subscription_id` — Razorpay's subscription reference.
- `subscriptions.razorpay_customer_id` — Razorpay's customer reference.
- `subscriptions.payment_method_last_four` — the last 4 digits only (display: "•••• 4242").
  Per PCI-DSS, the last 4 are not sensitive cardholder data. Populated from the Razorpay
  webhook payload (`card.last4`) — a surgical whitelist on the otherwise routing-only
  redacted payload (CL-390); see VT-330.

## What we NEVER store or log
- PAN (full card number), CVV/CVC, full expiry, the full Razorpay payment token, or any
  raw card data. None of these appear in `pipeline_log`, `admin_audit_log`,
  `privacy_audit_log`, or the `razorpay_webhook_events` inbox (its payload is redacted to
  routing-only fields — VT-89).

## Card capture flow (VT-91)
1. Owner reaches `/team/subscribe` (portal session) or via a trial-end deep-link
   (`?token=`, a short-lived audience-scoped JWT — VT-91 / issuance VT-332).
2. The page POSTs `{plan_tier}` to `/api/team/razorpay/subscribe`; the orchestrator
   (money-authoritative) creates the Razorpay subscription server-side.
3. Razorpay's HOSTED Checkout collects the card against that subscription. We receive only
   the subscription/customer ids back — never card data.
4. The first successful charge fires Razorpay's `payment.captured` webhook (HMAC-verified,
   VT-89), which is the SINGLE source that flips the phase to `paid_active`.

## Webhook authenticity
Inbound Razorpay webhooks are HMAC-SHA256 verified (`verifyRazorpaySignature`,
`RZP_WEBHOOK_SECRET_*`) before any state change. LIVE keys are NEEDS-FAZAL, hard-gated
(VT-93-N1 + VT-329 + VT-330 + VT-331-live).

## Sign-off
Fazal signs off on the PCI posture + the LIVE-key cutover.
