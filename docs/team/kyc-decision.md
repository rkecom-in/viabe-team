# Business-verification decision — Option F (VT-112 → VT-361)

**Status:** DECIDED + IMPLEMENTED (Fazal 2026-06-07/08). This doc closes **VT-112**; the build is **VT-361**.

## Decision
**Option F — instant programmatic business verification** at signup, via **Sandbox by Quicko**
(PAYG wallet). Two signals, composed:

1. **GSTIN lookup** (Sandbox public GSTIN search, no taxpayer auth) → the authoritative legal/trade
   name + status + constitution. Proves the business EXISTS. On its own it is *knowledge, not
   control* — anyone can type a public GSTIN.
2. **Reverse penny-drop name bind** (Fazal ruling 2026-06-08, reverse — NOT forward): the owner pays
   ₹1 via UPI; the vendor returns the **payer's bank-registered name**; we fuzzy-match it against the
   GSTIN/claimed name. Proves *control* of a bank account whose name matches the business. Collects
   **zero** financial data from the owner (no account number / IFSC) — strictly better DPDP posture
   than a forward penny-drop.

### Tiers
- `unverified` — default / vendor-down / no match (fail-closed).
- `name_verified` — reverse-penny-drop payer name matches the claimed business name (no GSTIN).
- `gstin_verified` — GSTIN-lookup name ∧ reverse-penny-drop payer name BOTH match (top tier). Both
  sides are vendor-authoritative → the owner cannot type their way to it (anti-gaming).

Proprietorships: the GSTIN legal name *is* the proprietor's personal name, so the proprietor's
penny-drop payer name matches it directly — handled by matching the payer against the stored
authoritative name.

## Rationale trail
- **Meta-binding insight:** the WhatsApp/Meta number is already a weak identity anchor, but not
  ownership proof. We need a control signal.
- **Razorpay-KYC debunk:** Razorpay onboarding KYC is *Razorpay's* merchant KYC, not reusable as our
  business-verification signal, and gates payments, not signup. Rejected as the verification source.
- **GSTIN + bind design:** GSTIN gives the authoritative name cheaply; the reverse penny-drop binds
  control without collecting bank data. Option F = the minimal-PII control proof.

## EVALUATED-AND-DEAD: GST-OTP bind
A GST-portal-OTP bind (the strongest proof) was evaluated and **rejected — no accessible API**
(Fazal 2026-06-08). The ASP/GSP onboarding path (Quicko GSP agreement + the owner's GST-portal
username — the "accountant problem") is not viable. **Not built; no Phase-2.**
**Single re-evaluation trigger:** an accessible GST-OTP API materializes.

## Architecture (VT-361)
- All vendor calls **orchestrator-side**, fail-closed, internal-secret (team-web proxies via
  `forwardBusinessVerification`). Endpoint: `POST /api/business-verification` (action =
  lookup | initiate | bind). Vendor client: `integrations/methods/sandbox_kyc.py`.
- **Owner-surface flow**, not a synchronous signup-txn step: signup doesn't collect a GSTIN and the
  reverse penny-drop is interactive (the owner pays ₹1), so verification runs as a post-signup
  owner-surface flow (lookup → initiate → bind), not inside `run_signup`.
- **Result-only storage** (mig 120): tenants.{verification_status, verified_business_name,
  verification_method, gstin, verified_at}. No documents, no payer names, no bank data. The payer
  name is matched then discarded. DSR scrubs gstin + verified_business_name; the per-tenant
  `kyc_verification_log` (attempt-cap + wallet-cost, no PII) is purged on DSR-delete.
- **Guards:** per-tenant-per-day attempt cap (no retry storms); wallet-cost category logged per call.
- **Gating** (mandatory-to-activate vs badge-only): Fazal's call (separate ruling). Storage + lookup
  + bind + badge are unconditional; the `transitions.py` activation gate is an add-on if Option A.

## Canary (Rule #15)
Real `search_gstin('27AAKCR3738B1ZE')` (Fazal's GSTIN, consented) once `.viabe/secrets/sandbox.env`
is filled → asserts the parsed name is the RKECOM-family. Until creds land it is a **gated
post-creds acceptance step (fail-not-skip)**; the reverse-penny-drop canary stays stubbed until a
test bank flow exists.
