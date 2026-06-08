# Business-verification decision — Option F, two-tier (VT-112 → VT-361)

**Status:** DECIDED + IMPLEMENTED (Fazal 2026-06-07/08). Closes **VT-112**; the build is **VT-361**.

## Decision (two-tier, Fazal ruling 2026-06-08)
**Instant programmatic business verification** at the owner surface, via **Sandbox by Quicko**
GSTIN lookup. Two tiers + a hard activation gate.

### Tiers
- `unverified` — default / vendor-down / GSTIN not-found-or-inactive. **Cannot activate.**
- `gstin_verified` ("yellow") — Sandbox `search_gstin` returns an **ACTIVE** GSTIN; the authoritative
  legal/trade name is stored. **Lookup success ALONE earns it — no ownership bind at launch.**
- `vtr_verified` ("green") — manual VTR/ops upgrade (audited). **No product significance yet**
  (gates nothing); value arrives in a later phase.

### Activation gate
`card_captured → paid_active` requires `gstin_verified` (or above). GSTIN-less businesses **cannot
activate — intended.** The gate reads `verification_status` **server-side** from the tenant row at
transition time (never a client field — IDOR lesson). Fail-closed on vendor outage (lookup fails →
unverified → activation waits; per-day retry; `vendor_down` logged distinctly from `invalid_gstin` so
ops can tell an outage from bad input). A blocked capture emits a distinct
`activation_blocked_verification` event (owner-surfaceable: "complete GSTIN verification to
activate") — not a silent stall.

## ACCEPTED RISK (Fazal, 2026-06-08): lookup without ownership bind
`gstin_verified` proves the GSTIN **exists + is active**, not that the signing owner **controls** it
— anyone can type a public GSTIN. Fazal explicitly accepts this impersonation residual at launch.
Backstops: (1) the **VTR "green" override** as a manual escalation path; (2) the Razorpay payment
instrument at activation is itself a (weak) control signal. Re-evaluate if abuse appears.

## EVALUATED-AND-DEAD
- **GST-OTP bind** — the strongest proof, but **no accessible API** (Fazal 2026-06-08). The ASP/GSP
  onboarding path (Quicko GSP agreement + the owner's GST-portal username — the "accountant
  problem") is not viable. Re-eval trigger: an accessible GST-OTP API materializes.
- **Domain-email / WHOIS / director-name matching** — rejected: spoofable (free email domains,
  privacy-proxied WHOIS) and persona-hostile (Tier-2/3 SMBs rarely have a business domain).

## DEFERRED-NOT-DEAD: reverse penny-drop (the one bind worth revisiting if abuse shows up)
A reverse penny-drop (owner pays ₹1; vendor returns the payer's bank-registered name; match it) would
add a control signal with **zero owner financial data collected**. Deferred, not dead. **Caveat
(fact-checked 2026-06-08):** Sandbox's KYC FAQ mentions "penny drop and reverse penny drop", but the
documented Bank Account Verification endpoint suite lists ONLY IFSC verification, **forward** penny
drop (₹1 micro-deposit — requires collecting account + IFSC), and Penny-Less. **No reverse-penny-drop
endpoint is documented.** If un-deferred: (a) confirm RPD availability with Sandbox support, OR
(b) use Sandbox **forward** penny drop (cost: collects owner account + IFSC — worse DPDP posture),
OR (c) an RPD-documenting vendor — Setu / Cashfree / Surepass / HyperVerge. **Do not assume "via
Sandbox" for the RPD path.**

## Rationale trail
- **Meta-binding insight:** the WhatsApp/Meta number is a weak identity anchor, not ownership proof.
- **Razorpay-KYC debunk:** Razorpay onboarding KYC is *Razorpay's* merchant KYC, not reusable as our
  business-verification signal, and gates payments not signup. Rejected as the source.
- **GSTIN lookup:** authoritative business name + existence, cheaply, no taxpayer auth. The two-tier
  ruling accepts lookup-alone for activation (yellow) with VTR (green) as the manual upgrade path.

## Architecture (VT-361)
- All vendor calls **orchestrator-side**, fail-closed, internal-secret (team-web proxies via
  `forwardBusinessVerification`). Endpoint `POST /api/business-verification` (lookup). Vendor client
  `integrations/methods/sandbox_kyc.py` (search_gstin only). VTR override on the ops surface
  (`/api/orchestrator/ops/vtr-verify`, operator-JWT + internal-secret, audited, tenant server-resolved).
- **Owner-surface flow** (GSTIN entry post-signup, pre-activation), NOT a synchronous signup step —
  signup collects no GSTIN. A signup-form GSTIN field is a possible later UX pass (noted in VT-361).
- **Result-only storage** (mig 120): tenants.{verification_status, verified_business_name,
  verification_method, gstin, verified_at}. No documents. DSR scrubs gstin + verified_business_name;
  the per-tenant `kyc_verification_log` (attempt-cap + wallet-cost, no PII) is purged on DSR-delete.
- **Guards:** per-tenant-per-day attempt cap (no retry storms); wallet-cost category logged per call.

## Canary (Rule #15)
Real `search_gstin('27AAKCR3738B1ZE')` (Fazal's GSTIN, consented) once `.viabe/secrets/sandbox.env`
is filled → asserts the parsed name is the RKECOM-family. Until creds land it is a **gated post-creds
acceptance step (fail-not-skip)**; VT-361 stays open until it runs.
