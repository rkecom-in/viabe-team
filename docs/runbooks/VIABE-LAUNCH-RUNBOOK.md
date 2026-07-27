# Viabe Team — Launch Runbook
*As of 2026-06-09. Target launch: 2026-07-15.*

> **⚠️ Dates and row-counts below are STALE as of 2026-07-17 — reconcile against `git log` + the sprint board before acting on any number here.**

## Where we are
Engineering is essentially complete (323/356 rows done). The dev→prod cutover is built and the Mumbai prod database is live and verified (121/121 migrations, RLS enforced). What remains is **provisioning, external approvals, and one prod smoke test** — almost none of it is code.

The whole launch hinges on **one root action: filing the Viabe trademark.** It unblocks the entire WhatsApp + login chain. Do it first.

---

## The critical path (what blocks what)

```
FILE Viabe trademark ──┬──> WABA display name "Viabe" (Meta)  ──> WhatsApp comms + WhatsApp-OTP login + VT-318
                       └──> SMS DLT header (Airtel)            ──> SMS-OTP login (backup channel)

Counsel legal copy ────────> privacy URL live ────────────────> WABA approval + DPDP compliance

Razorpay live cutover ─────> payments        (independent — ready now, post-Mumbai)
Apify prod ────────────────> the moat/ingestion (independent — cheap, token already set)
```

**Login works once EITHER WABA (WhatsApp OTP) OR DLT (SMS OTP) is live — and both gate on the trademark.** That is why the trademark is the keystone.

---

## PHASE 0 — Finish the technical cutover (this week; small)

| # | Action | Owner |
|---|---|---|
| 0.1 | Restore the shared git tree to `dev` (`git checkout dev`) | CC (queued) |
| 0.2 | Paste `FAZAL_TENANT_ID=d6f2510f-eae4-46ad-9c13-0f2fdc214191` + `FAZAL_OWNER_UUID=516efeb1-dad4-46af-b3f4-90c0811c81c5` into **both** Vercel prod + Railway prod | Fazal |
| 0.3 | Confirm `OPERATOR_JWT_SECRET` + `INTERNAL_API_SECRET` are **byte-identical** across Vercel ↔ Railway | Fazal |
| 0.4 | Fill remaining Vercel prod: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SITE_URL`, `OPERATOR_EMAIL` (confirm Supabase values are Mumbai) | Fazal |
| 0.5 | Apify actor-ID reconcile + minimal canary (VT-110) | CC (queued) |
| 0.6 | Authorize the first **dev→main promotion** (the CL-432 gate) | Fazal |
| 0.7 | Trigger the manual prod team-web deploy (`deploy-prod.yml` / workflow_dispatch) | Fazal |
| 0.8 | Prod **operator-login functional check** — proves the JWT/INTERNAL secrets match + UUIDs work end-to-end | CC |

When 0.8 passes, the production stack is genuinely live (no customers yet).

---

## PHASE 1 — File the trademark (do FIRST; days, not months)
- File the **Viabe** trademark; capture the **application number**.
- Both Meta (WABA name) and Airtel (DLT header) typically accept a *pending application number* — so this unblocks Phase 3 without waiting for the grant.
- If either rejects the pending application, fall back to the `RKECOM` interim SMS header so login isn't stranded.

## PHASE 2 — Legal / counsel (parallel with Phase 1)
- Get the **privacy notice + DPDP disclosure + terms** reviewed by counsel (VT-156 / VT-353).
- Publish the pages → **privacy URL live** (CC swaps the DRAFT shells for approved copy).
- This is required for WABA approval AND for DPDP launch compliance.

## PHASE 3 — WhatsApp + SMS go-live (gated on Phases 1 & 2)
**WABA (per-tenant WhatsApp — the core comms path):**
- Meta Business verification (your incorporation docs) + Twilio WhatsApp tech-provider/Embedded-Signup + the Meta/FB app.
- Provide `whatsapp.env` creds → CC flips the two deferred ES stubs (`_default_exchange`/`_default_provision`) → live E2E with one real merchant WABA.
- Flipping the per-tenant status to `live` unblocks **VT-318** (customer STOP/opt-out).

**SMS DLT (backup OTP/login channel):**
- Procure the header (Viabe-branded post-TM, or `RKECOM` interim) + whitelist the OTP template.
- CC enables `VT250_SMS_CHANNEL_ENABLED=1`.

## PHASE 4 — Payments + ingestion (independent; can run in parallel)
**Razorpay live (VT-109 / VT-89):**
- Create the 3 **live** Plans in the Razorpay dashboard.
- Put the live keys **directly into Railway/Vercel prod** (never local) — your hands.
- CC wires the live subscribe-create (VT-89) behind the fail-closed LIVE gate, then `TEAM_RAZORPAY_LIVE=1` + a live canary.

**Apify (VT-110):**
- Token is set. CC reconciles actor IDs + caps reviews + canary (in flight). Starter plan (~$29/mo) when you scale past the $5 test credit.

---

## PHASE 5 — Go-live
1. **Full prod E2E smoke** (CC, against Mumbai): real test tenant → OTP → connect data → ingest → agent brief → ₹ payment → opt-out. All green.
2. **Open signup:** flip the signup exposure gate live ONLY after confirming OTP-before-create + per-IP throttle are on (VT-82/96 — this is a hard security gate).
3. Announce / onboard the first founding tenants.

---

## The one-line answer
**File the Viabe trademark today.** Everything WhatsApp-and-login-shaped is waiting on it, and it likely only needs the application number. In parallel, get counsel moving on the privacy copy. Those two are the real launch critical path — the rest (prod env fills, Razorpay live, Apify) is quick and mostly mechanical.
