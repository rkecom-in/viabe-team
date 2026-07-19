---
title: Live-run config checklist — dev env flips for the fully-live e2e
authored: 2026-06-29
rule18: names-to-booleans-only — no secret values appear anywhere in this file
---

# Live-run config checklist

Rule 18 binding: this document lists VARIABLE NAMES and the boolean presence result.
No secret values. All presence checks must be run from a Railway-linked terminal.

---

## FLIP CHANGES (apply before the live run)

These are changes that must be made; current state assumed to be the "safe/off" default.

### Railway dev orchestrator (`vt-orchestrator-service`, env `dev`)

| Variable | Set to | What it does | Risk if wrong |
|---|---|---|---|
| `DEV_SEND_ALLOWLIST` | `+919321553267,+917738859946,+919892616965` | Opens real WhatsApp sends to Fazal's 3 internal numbers. Everything else stays mocked. The VT-476 dev send-guard reads this at call time (not cached). | Wrong numbers = real sends to wrong people. Empty = no real sends (safe but run fails). |
| `TEAM_TWILIO_VERIFY_MOCK_MODE` | `0` (or unset) | Disables the static-OTP bypass (mock mode returns approved for any code `123456`). Must be OFF for real OTP. | If left ON on dev, OTP is fake → no proof of number control. The VT-434 prod boot guard blocks `=1` on EXPECTED_ENV=prod, so this is a dev-only risk. |
| `TEAM_TWILIO_MOCK_MODE` | `0` (or unset) | Disables the Twilio send mock (which drops all sends silently). Must be OFF for real WhatsApp. | If left ON, the `team_welcome2` + every subsequent owner WA message is silently dropped. The journey never starts. |
| `TEAM_SANDBOX_GST_MOCK_MODE` | `0` (or unset) | Disables the GST mock fixture. Real Sandbox.co.in API calls required for the GSTIN hard-gate. | If left ON, GST verify is faked — the real GSTIN chain (knowyourgst → Sandbox confirm) doesn't run. Not a safety risk on dev; breaks the authenticity of the e2e. |

### Vercel `viabe-team-web-dev`

| Variable | Set to | What it does | Risk |
|---|---|---|---|
| `ENABLE_PUBLIC_SIGNUP` | `true` | Opens the `/api/team/signup` route (currently 404-gated). | Off = no signup possible. |
| `NEXT_PUBLIC_TEAM_LAUNCH_MODE` | `live` | Build-time toggle — renders the signup form instead of the waitlist page. Requires Vercel redeploy after setting. | Off/`waitlist` = signup page redirects to /team. No form visible. |

---

## CONFIRM-SET (presence check — names→booleans only)

These should already be set on Railway dev from prior wiring. Run the presence check from a
Railway-linked terminal using `scripts/env_presence.py`.

**Command to run (from a Railway-linked terminal):**
```
python3 scripts/env_presence.py presence --source railway --environment dev --service vt-orchestrator-service \
  TWILIO_VERIFY_SERVICE_SID SANDBOX_API_KEY SANDBOX_API_SECRET SCRAPINGBEE_API_KEY \
  TEAM_TWILIO_FROM_NUMBER INTERNAL_API_SECRET
```

Expected output (all `set`):
```
TWILIO_VERIFY_SERVICE_SID: set
SANDBOX_API_KEY: set
SANDBOX_API_SECRET: set
SCRAPINGBEE_API_KEY: set
TEAM_TWILIO_FROM_NUMBER: set
INTERNAL_API_SECRET: set
```

**Sealed-vars caveat (from memory: 2026-06-25):** ALL Railway and Vercel secrets are SEALED.
`env_presence.py` reads them via `railway variables --json` which returns the sealed-token form —
Railway intentionally omits the VALUE for sealed vars, and the script may report them as `unset`
(false negative). This is a known limitation. If a presence check returns `unset` for a var that
was deliberately set by Fazal, **verify by USE, not by presence**:

- `TWILIO_VERIFY_SERVICE_SID`: canary — run signup OTP and see if a real WhatsApp arrives
- `SANDBOX_API_KEY` / `SANDBOX_API_SECRET`: canary — enter GSTIN `27AAKCR3738B1ZE` and verify it resolves
- `SCRAPINGBEE_API_KEY`: verify-by-use — enter "RKeCom" as business name and check if GSTIN candidates appear
- `TEAM_TWILIO_FROM_NUMBER`: verify-by-use — check that the dev sender `+918108084223` is configured (per `.viabe/templates.md`)

### Vercel `viabe-team-web-dev` presence

```
python3 scripts/env_presence.py presence --source railway --environment dev --service viabe-team-web-dev \
  TEAM_TWILIO_AUTH_TOKEN INTERNAL_API_SECRET TEAM_ORCHESTRATOR_URL
```

Expected: all `set`. Same sealed-vars caveat applies.

---

## Template registry check

`team_winback_simple` status in `config/twilio_templates.yaml`:
- `agent_selectable: true` — CONFIRMED (checked in this session)
- `category: customer_marketing` — CONFIRMED
- `en` SID: `HX601925a292da89e9d00d3fdf8742f765` — CONFIRMED (Meta status `approved` per VT-383 canary)
- `hi` SID: `HX5da4406f8a6691f52555cd179f40be73` — CONFIRMED

WABA sender for dev: `+918108084223` (plain E.164 in env, `whatsapp:` prefix applied by `_wa()` at send site).
Twilio sender SID: `XE5dca19b08f04ba5e11d69735c6969a9d`. Status: ONLINE per `.viabe/templates.md`.

---

## Env flip risk matrix

| Flag | Safe default | Live-run value | Revert after run? |
|---|---|---|---|
| `DEV_SEND_ALLOWLIST` | `""` (empty = mock all) | 3 allowlisted numbers | YES — clear after run to restore fail-closed default |
| `TEAM_TWILIO_VERIFY_MOCK_MODE` | `1` or `0` (check) | `0` | NO — `0` is the correct dev state post-activation |
| `TEAM_TWILIO_MOCK_MODE` | `1` or `0` (check) | `0` | NO — `0` is the correct dev state post-activation |
| `TEAM_SANDBOX_GST_MOCK_MODE` | `1` or `0` (check) | `0` | NO — `0` is the correct dev state for GST-live testing |
| `ENABLE_PUBLIC_SIGNUP` | `false` (not set) | `true` | NO — stays on for ongoing dev testing |
| `NEXT_PUBLIC_TEAM_LAUNCH_MODE` | `waitlist` | `live` | NO — stays `live` for dev |

---

## Note on the coordinator cron

`AGENT_COORDINATOR_CRON = "0 10 * * *"` → daily at 10:00 UTC = 3:30 PM IST.

`kick_coordinator(tenant_id)` exists in `coordinator.py:299` but is **NOT wired to an HTTP
endpoint**. For the live run, the options are:

1. Wait for the 10:00 UTC cron window (after the tenant is eligible)
2. CC adds a POST `/api/orchestrator/coordinator/kick` endpoint before the run (recommended)

This is flagged as a gap in the VT-431 plan. Needs resolution before Fazal can do a same-day
win-back run without waiting for the cron window.
