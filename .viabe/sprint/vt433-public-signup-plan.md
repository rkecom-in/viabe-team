---
plan_for: VT-433
title: Enable dev public signup (ENABLE_PUBLIC_SIGNUP) + inbound webhook dev-secrets
status: READY-FOR-GATE
risk: PII / inbound (CL-422 internal-only constraint)
authored: 2026-06-29
---

# VT-433 — Dev public signup + inbound webhook dev-secret wiring

## What this enables

Turning on `ENABLE_PUBLIC_SIGNUP=true` + `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live` opens the
`/team/signup` route so the real owner OTP→GSTIN→ownership→tenant-create flow can be driven
end-to-end against dev. The inbound webhook secrets make the Twilio inbound webhook (WhatsApp
→ orchestrator) validate correctly so the onboarding journey converses over real WhatsApp.

**CL-422 hard rule (unchanged):** dev signup uses ONLY Fazal-controlled INTERNAL identities.
No fabricated numbers. No real external customer data on Seoul dev. The identities for the
live run are Fazal's internal allowlist: +919321553267, +917738859946, +919892616965.

---

## Gate summary

- **Config flags** (CC can set on dev, no code change):
  - `ENABLE_PUBLIC_SIGNUP=true` → Vercel `viabe-team-web-dev`
  - `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live` → Vercel `viabe-team-web-dev` (triggers rebuild — required for
    `launchMode()` which is a build-time `process.env` read; a Vercel redeploy must follow)
- **Secrets** (Fazal-set; CC relays the var NAMES only, never values):
  - `TEAM_TWILIO_AUTH_TOKEN` → Vercel `viabe-team-web-dev` (note: the code reads this name, NOT
    bare `TWILIO_AUTH_TOKEN` — the VT-433 row named it wrong; the correct name is `TEAM_TWILIO_AUTH_TOKEN`)
  - `RAZORPAY_WEBHOOK_SECRET` → Vercel `viabe-team-web-dev` (the VT-433 row listed `RZP_WEBHOOK_SECRET` /
    `TEAM_RAZORPAY_WEBHOOK_SECRET`; the canonical no-env-suffix rule applies: check `apps/team-web/.env.example`
    for the exact name before CC sets this)
  - `SHOPIFY_API_SECRET` → Railway dev orchestrator (VT-422 HMAC; Fazal-provided)
  - `TELEGRAM_OPS_WEBHOOK_SECRET` → Vercel `viabe-team-web-dev` (CC-dev-set is acceptable)
  - `INTERNAL_API_SECRET` → both Vercel and Railway dev (CC-dev-set, shared between team-web and orchestrator)

---

## Change-by-change breakdown

### 1. `ENABLE_PUBLIC_SIGNUP=true` (Vercel `viabe-team-web-dev`)

**What it does:** The `/api/team/signup` route (`apps/team-web/app/api/team/signup/route.ts:21`) is
hard-gated: `if (process.env.ENABLE_PUBLIC_SIGNUP !== 'true') return 404`. With the flag off, the
signup route is a dead 404 regardless of what the UI does. Flipping it to `true` opens the
OTP-before-create → GSTIN-verify → ownership-OTP → tenant-create flow.

**Risk:** Opens a real sign-up surface on dev Seoul. Constrained to internal identities by CL-422.
The OTP gate (Twilio Verify) + per-IP rate limit are structural — no number squatting.

### 2. `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live` (Vercel `viabe-team-web-dev`)

**What it does:** `launchMode()` in `lib/launch-mode.ts` is a `NEXT_PUBLIC_*` (build-time) env var.
Default when absent is `'waitlist'`. When `'waitlist'`, the `/team/signup/page.tsx` server component
does `redirect('/team')` immediately — the signup form is never rendered. Setting this to `'live'`
lets the signup page render the `<SignupForm />`.

**Critical:** because `NEXT_PUBLIC_*` is baked at build time, a Vercel environment variable change
here REQUIRES a Vercel redeploy to take effect. Simply setting the variable in the Vercel dashboard
and refreshing the page won't work. Trigger a redeployment (Vercel dashboard → Deployments →
Redeploy, or push a no-op commit to dev).

**Risk:** `'live'` mode renders the full signup form. On dev, this is acceptable under CL-422.

### 3. `TEAM_TWILIO_AUTH_TOKEN` (Vercel `viabe-team-web-dev`)

**What it does:** `apps/team-web/lib/twilio.ts` calls `verifyTwilioSignature` using
`process.env.TEAM_TWILIO_AUTH_TOKEN` (not `TWILIO_AUTH_TOKEN` — the code is explicit, and the
test at `tests/api/twilio-webhook.test.ts:133` asserts that the bare unprefixed name is rejected).
Without this, `verifyTwilioSignature` returns false and every Twilio inbound webhook is rejected
(no WhatsApp replies flow through to the orchestrator).

**Risk:** Twilio auth token is a credential. Fazal sets the value; CC never reads/logs it.

### 4. `INTERNAL_API_SECRET` (both Vercel and Railway dev)

**What it does:** team-web passes this as `X-Internal-Secret` header when calling the orchestrator's
BYPASSRLS endpoints (`/api/signup`, `/api/waitlist`). The orchestrator verifies it via
`hmac.compare_digest`. If either side is missing or mismatched, signup returns 403.

**Risk:** A shared internal secret between two services. CC can generate a random value (e.g.
`openssl rand -hex 32` via subshell substitution — never print to stdout). Set the SAME value on
both Vercel and Railway dev.

### 5. `TWILIO_VERIFY_SERVICE_SID` (Railway dev orchestrator)

**What it does:** `apps/team-orchestrator/src/orchestrator/auth/twilio_verify.py:116` reads this
to start/check OTP verifications. Without it, `VerifyServiceSidMissingError` is raised and all
OTP calls fail. This powers BOTH the signup OTP AND the ownership OTP.

**Risk:** None if already set. If absent on Railway dev, signup fails at first OTP step.

### 6. `SANDBOX_API_KEY` + `SANDBOX_API_SECRET` (Railway dev orchestrator)

**What it does:** `orchestrator/integrations/methods/sandbox_kyc.py` uses these to call the
Sandbox.co.in GSTIN verify API. Required for real GSTIN verification (VT-408 hard gate — a
missing/empty GSTIN or a failed verify blocks tenant creation). With `TEAM_SANDBOX_GST_MOCK_MODE=0`
(live run), these MUST be set.

**Risk:** Live Sandbox calls consume API quota. Canary GSTIN `27AAKCR3738B1ZE` is the known-good
test fixture.

### 7. `SCRAPINGBEE_API_KEY` (Railway dev orchestrator)

**What it does:** VT-495 `KnowYourGSTScraper.search()` uses ScrapingBee to discover GSTIN
candidates from knowyourgst.com. Without it, the discovery step fails-open → falls through to
manual GSTIN entry. The flow doesn't BREAK without this (manual entry is the fallback), but the
e2e is more faithful with it. For the live run: desirable but not blocking.

### 8. Razorpay / Shopify / Telegram webhook secrets

These are inbound webhook validators. For the signup→onboarding→win-back live run, only the Twilio
inbound webhook (`TEAM_TWILIO_AUTH_TOKEN`) is on the critical path. Razorpay (payment webhook),
Shopify (integration), and Telegram (ops) are NOT on the signup→win-back journey path. They are
listed in VT-433's contract and should be set for completeness, but a live run that stops at
win-back approval doesn't strictly need them.

---

## Sequencing

All items below are env-only (no code change):

1. CC sets: `ENABLE_PUBLIC_SIGNUP=true`, `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live`, `INTERNAL_API_SECRET`
   (fresh random) on Vercel `viabe-team-web-dev` AND `INTERNAL_API_SECRET` (same value) on Railway dev.
2. Fazal sets (or confirms set): `TEAM_TWILIO_AUTH_TOKEN` on Vercel, `TWILIO_VERIFY_SERVICE_SID` +
   `SANDBOX_API_KEY` + `SANDBOX_API_SECRET` on Railway dev.
3. Trigger Vercel redeploy (mandatory for `NEXT_PUBLIC_*` to take effect).
4. Canary: open `/team/signup` on the dev Vercel URL → should render the signup form (not redirect
   to /team). Enter Fazal's number → OTP should arrive via WhatsApp.

---

## What is NOT a code change

All of VT-433 is env-only (flag flips + secrets). No code changes are needed. The gates are already
in place (`ENABLE_PUBLIC_SIGNUP` runtime check in team-web; `TWILIO_VERIFY_SERVICE_SID` required-or-raise
in twilio_verify.py).
