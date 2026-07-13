# Fully-Live e2e Runbook — Win-back to +917738859946

**Date authored:** 2026-06-29
**Env:** Dev (Seoul, Railway dev + Vercel viabe-team-web-dev)
**Rule:** Do NOT advance to step N+1 until step N passes. On fail → stop, diagnose.

## What is real in this run

| Component | Real or adapted |
|---|---|
| Signup OTP (WA) | REAL — Twilio Verify, no mock |
| GSTIN discovery (knowyourgst + Sandbox) | REAL — ScrapingBee + live Sandbox API |
| Ownership OTP | REAL — Twilio Verify to Fazal-entered number |
| GBP phone discovery | ADAPTED — RKeCom GBP unreliable; Fazal enters manually |
| Onboarding journey (WhatsApp) | REAL — real messages to +919321553267 |
| Coordinator dispatch | REAL — autonomous sweep (or kick endpoint once wired) |
| SR draft + L2 approval ask | REAL — `team_agent_draft_approval` WA to +919321553267 |
| Win-back send | REAL — `team_winback_simple` to +917738859946 (allowlisted) |

---

## Pre-flight (must all be true before Step 1)

| Check | Expected | How to verify |
|---|---|---|
| Vercel dev URL serves `/team/signup` as a FORM (not redirect to /team) | Signup form renders | Open URL in browser — if you see the landing page/waitlist, `NEXT_PUBLIC_TEAM_LAUNCH_MODE` is not active yet → trigger Vercel redeploy |
| `TEAM_TWILIO_MOCK_MODE=0` on Railway dev | Real WA sends go out | Checked by config checklist |
| `TEAM_TWILIO_VERIFY_MOCK_MODE=0` on Railway dev | Real OTPs sent | Checked by config checklist |
| `TEAM_SANDBOX_GST_MOCK_MODE=0` on Railway dev | Real GST verify | Checked by config checklist |
| `DEV_SEND_ALLOWLIST` contains all 3 numbers | Real sends to allowlisted only | Checked by config checklist |
| `TWILIO_VERIFY_SERVICE_SID` set on Railway dev | OTP calls resolve | Verify by use at Step 1c |
| `SANDBOX_API_KEY` + `SANDBOX_API_SECRET` set on Railway dev | GST verify works | Verify by use at Step 1b |
| `TEAM_TWILIO_AUTH_TOKEN` set on Vercel dev | Twilio inbound webhook validates | Verified when WA inbound flows at Step 2b |
| `INTERNAL_API_SECRET` set on BOTH Vercel and Railway dev (same value) | Signup proxy can call orchestrator | Verified at Step 1d if create succeeds |

---

## Phase 1 — Signup

### Step 1a — Open the signup form

Open the dev Vercel URL at `/team/signup` in a browser (NOT the WA in-app browser — a desktop
or mobile real browser).

**Pass:** The signup form is visible with fields for name, phone, GSTIN.
**Fail:** Redirects to the landing page. → `NEXT_PUBLIC_TEAM_LAUNCH_MODE=live` not active.
Trigger Vercel redeploy (dashboard → Deployments → Redeploy).

### Step 1b — Enter details + GSTIN discovery

- **Business name:** RKeCom (or the name registered under GSTIN `27AAKCR3738B1ZE`)
- **WhatsApp number:** `+919321553267`
- **GSTIN:** leave blank initially if the form offers name-based discovery; OR enter `27AAKCR3738B1ZE` directly

If name-based discovery is live (VT-495 ScrapingBee + knowyourgst): the form searches for
"RKeCom" and shows candidate GSTINs. Select `27AAKCR3738B1ZE`.

If discovery fails (ScrapingBee miss, fallback to manual entry): the form shows a manual GSTIN
field. Enter `27AAKCR3738B1ZE`.

After GSTIN is entered: the form runs Sandbox.co.in verify. GSTIN `27AAKCR3738B1ZE` must return
`status = ACTIVE`. This is the VT-408 hard gate — no GSTIN, no tenant.

**Pass:** Form advances past GSTIN verify.
**Fail → sandbox API error:** Check `SANDBOX_API_KEY` / `SANDBOX_API_SECRET` on Railway dev.
Note: LIVE Sandbox creds are required (TEST creds 403 the GST endpoint, per earlier e2e learnings).

### Step 1c — Signup OTP (WhatsApp to +919321553267)

After GSTIN verify, the form triggers a Twilio Verify OTP to `+919321553267` via WhatsApp.

**What you receive on +919321553267:** A WhatsApp from the Viabe Team number (`+918108084223`)
with a 6-digit verification code.

Enter the code in the form. On approval, a `verified-number-proof` JWT is minted (short-lived).

**Pass:** Code accepted, form advances to the ownership step.
**Fail → no WA received:** Check `TWILIO_VERIFY_SERVICE_SID` on Railway dev + `TEAM_TWILIO_VERIFY_MOCK_MODE=0`.
**Fail → code rejected:** Wait — codes expire in ~10 min. Re-send if needed.

### Step 1d — Ownership OTP (prove you control the business number)

The ownership step (`/team/signup` → ownership-step.tsx) proves you control the PUBLIC business
number (separate from the personal signup WA).

**RKeCom GBP phone situation:** The GBP-discovered phone for RKeCom is unreliable. The form
will likely show the manual-entry input: "We couldn't find a public number — enter the one
customers call."

**Fazal's action:** Enter a number from the dev send allowlist:
- Use `+919321553267` (same as signup number — permitted by design; the ownership OTP proves
  you CONTROL the listed public business number, which is your WA number in this case)
- OR use `+917738859946` or `+919892616965` if you want a different number

Hit "Send code to my business number" → Twilio Verify OTP fires to the entered number.

Enter the code → `owner_channel_verified = true` → tenant is created.

**Pass:** Ownership verified. Tenant exists in dev Supabase Seoul with:
- `verification_status = 'gstin_verified'`
- `owner_channel_verified = true`
- `phase = 'trial_active'`

**Fail → OTP not received:** Check `TWILIO_VERIFY_SERVICE_SID` + that the entered number is
reachable on WA.

---

## Phase 2 — Welcome message + onboarding journey

### Step 2a — team_welcome2 arrives on +919321553267

Immediately after signup completes, the orchestrator sends the welcome template.

**You receive (on +919321553267):**
```
Hi [owner_name], your Viabe Team account is now active. Your trial period ends on [trial_end_date].
[Copy inviting a reply — the VT-404 fix; the old welcome said WAIT, this one says REPLY]
```

Template: `team_welcome2` (en, SID `HX65602e94b48bb2d6e82c70630d01da20`)

**Pass:** WA message arrives from `+918108084223` within ~60 seconds.
**Fail → no message:** Check `TEAM_TWILIO_MOCK_MODE=0` and `DEV_SEND_ALLOWLIST` includes +919321553267.

### Step 2b — Reply to open the 24h window

Reply ANYTHING to the welcome message. A bare "hi" or "ok" is fine. This:
- Opens the Twilio 24h session window (required for free-form messages back to you)
- Triggers the orchestrator's inbound handler → `maybe_handle_journey_reply`
- Queues the first onboarding question

The Twilio inbound webhook lands at the dev Vercel URL (`/api/team/twilio/webhook`) — validated
with `TEAM_TWILIO_AUTH_TOKEN`.

**Pass:** Within ~30 seconds, you receive the first journey question (or a confirm prompt with
Yes/No/Skip buttons).
**Fail → 403 on inbound:** `TEAM_TWILIO_AUTH_TOKEN` not set on Vercel or doesn't match Twilio's
account auth token.

### Step 2c — Answer journey questions

Questions arrive ONE AT A TIME over WhatsApp. Answer each one:

- **CONFIRM questions** (quick-reply buttons — `onboarding_confirm_yesno`): Tap YES / NO / SKIP
- **Free-form questions:** Type your answer and send

Typical journey for RKeCom (may vary based on GBP data found):
1. "We've identified you as [business_type] — is that right?" → YES (or correct it)
2. Business hours → confirm or provide
3. Service area / location → confirm
4. Customer segment → confirm or describe
5. Other reasoned-gap questions from the 2b layer

Keep answering until the journey completes. A completion message or absence of further questions
signals the journey is `status = 'complete'`.

**Pass:** `onboarding_journey` row in Supabase dev has `status = 'complete'`.
**Fail → journey stuck / same question repeating:** Check for VT-477/VT-503 defects in the
journey stall fix (this branch should have those fixes already).

---

## Phase 3 — Data source + customers

For the SR agent to detect win-back candidates, you need customers with purchase history in dev.

### Step 3a — Connect a data source

Options:
- **Google Sheets connector:** Wire a sheet with customer data (name, phone, last visit date)
  via the Ops Console or the conversational onboarding flow (the agent may prompt for this).
- **Shopify connector:** If `kk4xva-di.myshopify.com` is available, use the earlier e2e
  runbook's Shopify OAuth flow (see `docs/e2e-sundaram-runbook.md` Step 5 for that path).
- **Manual seed (fastest):** CC can seed `customers` + `customer_ledger_entries` for the
  tenant directly in Supabase dev — mark one customer (e.g. +917738859946) as lapsed
  (last visit >60 days ago). This bypasses the connector but lets the SR detect + win-back
  fire immediately.

**Pass:** `tenant_connector_status` shows a connected + pulled connector AND `customers` table
has ≥1 row for the tenant. Confirm customer +917738859946 is in the customers table (the
intended win-back recipient, on the allowlist).

### Step 3b — Confirm business plan exists

After journey completion, the Gap-4 seam generates a 6-month business plan. Check:
- `business_plan` table has rows for the tenant
- At least one row: `owning_agent = 'sales_recovery'`, `status = 'accepted'`

If the plan hasn't generated yet, wait a few minutes (it's a DBOS async workflow).

---

## Phase 4 — Autonomous dispatch (coordinator sweep)

### Step 4a — Trigger the coordinator

**Option A — wait for the cron:** `AGENT_COORDINATOR_CRON = "0 10 * * *"` (10:00 UTC = 3:30 PM IST).
If the test is run before that window, wait.

**Option B — kick endpoint (if available):** If CC has added POST
`/api/orchestrator/coordinator/kick` (see VT-431 gap note in the plan doc):
```
curl -X POST https://[dev-railway-url]/api/orchestrator/coordinator/kick \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: [INTERNAL_API_SECRET]" \
  -d '{"tenant_id": "[your-tenant-uuid]"}'
```
Find your tenant UUID in Supabase dev `tenants` table.

The sweep checks:
1. `AGENT_AUTONOMY_GLOBAL_FREEZE` — must not be set
2. `_owner_inputs_enabled(tenant_id)` — must be true (owner gave consent at signup)
3. No open `pending_approvals` (none yet)
4. Business plan has `sales_recovery` items in `accepted` status
5. `is_frozen(tenant_id, 'sales_recovery')` — false (PR-2 not deployed)

If all pass → dispatches ONE work item → `agent_dispatch_workflow` starts in DBOS.

**Pass:** Railway dev logs show `agent_coordinator_sweep` with `dispatched >= 1`.
**Fail → `dispatched = 0, skipped_not_onboarded > 0`:** SR eligibility gates not met. Check
journey completion, connector, customer count.
**Fail → `dispatched = 0, skipped_no_owner_inputs > 0`:** `owner_inputs` consent not set.
Check the tenant's `owner_inputs` flag in `tenants` table.

### Step 4b — SR executor runs (inside DBOS workflow)

`SalesRecoveryAgent.execute_item()` runs:
1. Re-checks `tenant_is_sr_eligible` (same gates as above)
2. Dormant customer DETECT — finds +917738859946 as lapsed (>60 days since last visit/purchase)
3. Drafts `team_winback_simple` messages (LLM via Anthropic API)
4. Arms L2: inserts into `pending_approvals` + `agent_drafts`
5. Sends `team_agent_draft_approval` to you on +919321553267

**Pass:** You receive the approval ask on +919321553267.

### Step 4c — Approval message received on +919321553267

**You receive:**
```
Hi [owner_name], your Viabe assistant has prepared [N] customer message(s) for your approval.
Sample: "Hi [customer_name], this is a message from [business_name]. We haven't seen you
in a while and we'd love to welcome you back — your favourites are waiting for you.
Visit us or reply here and we'll help you right away.
Reply STOP to stop receiving these messages."
Reply YES to approve and send, EDIT to change, or NO to reject.
Nothing is sent without your approval.
```

Template: `team_agent_draft_approval` (en, SID `HX1fa31e0339d5739d7936e6edf39e08a3`)

Note: the SAMPLE in the approval message shows the actual draft for +917738859946. The
`customer_name` and `business_name` vars are filled from the fact bundle.

**This is the FIRST send / decaying-checkpoint behavior (VT-474).** Every first campaign
for a tenant hits L2 (owner must approve before any customer send). Approved sends accumulate
toward the 20-clean streak → L3 autonomy offer (much later).

---

## Phase 5 — Win-back send

### Step 5a — Approve: reply YES

Reply **YES** to the `team_agent_draft_approval` message.

The inbound YES routes through the L2 reconciler sweep (`l2_approved_send_sweep_scheduled`)
→ `l2_send_workflow` starts → resolves drafts → calls `customer_send.agent_send_draft`.

### Step 5b — VT-476 send-guard check

The send path hits the dev send-guard at the Twilio client level. The target number
(+917738859946) must be in `DEV_SEND_ALLOWLIST`. It is (pre-flight check).

The guard logs: `[VT-476 dev-send-guard] ALLOWLISTED dev send -> ..8946 (real Twilio)`

### Step 5c — Win-back arrives on +917738859946

**You receive (on +917738859946):**
```
Hi [customer_name], this is a message from [business_name]. We haven't seen you in a while
and we'd love to welcome you back — your favourites are waiting for you. Visit us or reply
here and we'll help you right away.
Reply STOP to stop receiving these messages.
```

Template: `team_winback_simple` (en, SID `HX601925a292da89e9d00d3fdf8742f765`)
Sender: `whatsapp:+918108084223`

**PASS = the run is complete. G35 closed.**

---

## Verification checklist (post-run)

Supabase dev Seoul (`ap-northeast-2`):

| Table | What to confirm |
|---|---|
| `tenants` | `owner_channel_verified = true`, `verification_status = 'gstin_verified'`, `phase = 'trial_active'` |
| `onboarding_journey` | `status = 'complete'` |
| `business_plan` | row with `owning_agent = 'sales_recovery'`, status progressed |
| `agent_work_items` | row with `status = 'sent'` |
| `pipeline_runs` | row with `run_type = 'agent_dispatch'`, `status = 'completed'` |
| `pending_approvals` | row with `status = 'approved'` |
| `send_idempotency_keys` | row for the win-back send (exactly-once guard) |

Railway dev logs:
- `agent_coordinator_sweep` event: `dispatched >= 1`
- `[VT-476 dev-send-guard] ALLOWLISTED dev send -> ..8946 (real Twilio)`
- `l2_send_workflow` completion

Physical device (+917738859946): real WhatsApp win-back message received from +918108084223.

---

## Post-run cleanup

- **`DEV_SEND_ALLOWLIST`:** clear after run (restores fail-closed: empty = mock all sends)
- **`ENABLE_PUBLIC_SIGNUP`:** leave ON (dev testing)
- **`NEXT_PUBLIC_TEAM_LAUNCH_MODE`:** leave `live` (dev)
- **Mock mode flags:** leave at `0` (correct dev state post-activation)
