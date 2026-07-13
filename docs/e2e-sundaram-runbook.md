# Sundaram Book Store — E2E Test Runbook
### Sales Recovery via the conversational onboarding (WhatsApp-first)

**Env:** Dev @ `910ebc2` (VT-447 live — onboarding pulls real *orders*) · **Signup (live, verified):** https://viabe-team-dev.vercel.app/team/signup
**Tenant:** RKeCom Services Pvt Ltd · **Owner WhatsApp = +919820463598** (gets welcome + approval prompts; NOT in the seed) · **customer/recipient = +919321553267** (seeded lapsed customer, gets the win-back at Step 9) · seeded fillers = +917738859946, +919892616965 · **store = `kk4xva-di.myshopify.com`**.
**Rule:** Do **not** advance to step N+1 until step N's **Expected Outcome = PASS**. On FAIL → stop, diagnose, fix, re-run the step.
**Hard safety:** the ONLY number any message may reach is **+919321553267** (Fazal-provided). No fabricated numbers. `main`/prod untouched throughout.

> **Status as of 2026-06-26 08:15Z — Phase 0 is CC-complete; only Fazal's run-time items remain.** VT-447 (onboarding→orders fix) is live, the store is seeded, and a real-path detection dry-run confirms **+919321553267 is the sole candidate**. Remaining before Step 1: 0.5 (your real GSTIN, entered at Step 1) and 0.4 (connect exactly `kk4xva-di.myshopify.com` at Step 5). One thing to keep in your pocket for Step 5: the Partner app's redirect URL must match our callback (we set both to the Railway orchestrator URL).

---

## Phase 0 — Pre-flight (ALL must be GREEN before Step 1)

**Legend:** ✅ green · 🔄 in progress (CC) · ⚠️ needs Fazal

| # | Prerequisite | Owner | PASS criterion | Status |
|---|---|---|---|---|
| 0.1 | Deploy live (incl. VT-447 orders-fix) | CC | both deploys green + Railway `/health` ok at the VT-447 SHA | ✅ GREEN (`910ebc2`) |
| 0.2 | Public signup serves | CC | `/team/signup` returns **200** (flags set + live build) | ✅ live |
| 0.3 | Sealed creds set | Fazal set / CC verify | Razorpay **TEST** keys + Shopify key/secret (re-set) + `RAZORPAY_WEBHOOK_SECRET` set; proven functionally at run | ✅ set (functional check at run) |
| 0.4 | Same store for seed **and** connect | Fazal (at Step 5) | **Nothing to prep.** At Step 5, when the agent asks for your store, enter **`kk4xva-di.myshopify.com`** (the store CC seeds) + approve | ℹ️ Step-5 instruction (no pre-action) |
| 0.5 | GST verify creds | Fazal | dev `SANDBOX_API_KEY` + `SANDBOX_API_SECRET` = **LIVE** creds (TEST creds 403 the GST endpoint) **+ a valid GSTIN ready** for Step 1 | ✅ LIVE creds added (CC functional-confirms) |
| 0.6 | Store seeded — real **orders**, backdated | CC (Fazal's 3 numbers) | 3 customers + backdated **orders** in `kk4xva-di`: **+919321553267** ~90d/₹4000 (lapsed) + fillers +917738859946 ~12d, +919892616965 ~25d (recent); detect-shape re-check surfaces +919321553267 | ✅ GREEN — seeded; dry-run detects +919321553267 only (1 candidate, 3 sales) |
| 0.7 | Marketing consent | CC (number is Fazal-provided) | `MARKETING_CONSENT_VERSIONS=v1_test_e2e` set ✓; consent record for **+919321553267 only**, seeded right after Step 1 | ✅ version set; record @ Step 1 |

> If any 0.x is not GREEN, Step 1 does not start. **Active blockers now:** 0.1/0.6 (CC re-deploy + seed, in flight) and 0.4/0.5 (Fazal). 0.7's record is seeded after Step 1 by design (it's tenant-keyed).

---

## The journey — gated steps

| Step | Action (who) | Expected Outcome = PASS | How we verify |
|---|---|---|---|
| **1. Signup + GST hard-gate** | Fazal: open the URL, enter business + **GSTIN** + his internal phone, submit | GST verified live → **valid GST passes the hard-gate** → tenant created, journey started. (A no-GST/invalid entry would be **rejected** — the gate.) | CC: tenant row exists + GST-verified flag; Fazal: lands on the next screen |
| **2. Welcome WhatsApp** | (automatic) | `team_welcome` WhatsApp lands on his internal number (~1 min) | Fazal: message on device; CC: outbound send logged + delivered |
| **3. Profile-confirm journey** | Fazal: reply on WhatsApp | VT-367 journey runs — shows the **discovered business profile**, asks to confirm/correct → **completes** | CC: journey `status=complete`; chat shows the confirm step |
| **4. Seam → connect nudge** | (automatic on 3) | Integration agent nudges **"connect your store"** with the **Shopify link-out** (`authorize_url`) | Fazal: WhatsApp shows the connect link |
| **5. Shopify OAuth install** *(first live canary)* | Fazal: open the link in the WA browser → **approve** on his dev store | OAuth completes → **encrypted** `tenant_oauth_tokens` row (`expires_at` NULL) → webhooks registered | CC: token row present + decryptable; `shopify_is_connected=true`; `GET webhooks.json` lists `orders/create` |
| **6. Resume → pull + ingest** | Fazal: send another WhatsApp message | VT-267 resume re-checks connected → **real `pull_orders` (VT-447: real orders, not checkouts)** → auto-map → **ingest** (names-only/counts-only) → agent replies **"imported N customers"** | CC: `customers` + `customer_ledger_entries(entry_type='sale')` rows from the **orders**; **+919321553267 ingested** with its ~90d-old sale; logs counts-only (no raw PII) |
| **7. SR detect** | (automatic — coordinator now wired) | VT-421 eligibility **passes** (journey-complete + connected + customers≥1 + GST-verified) AND **+919321553267 is detected as a lapsed candidate** (recency/spend/consent/suppression all pass) | CC: detect result includes +919321553267; eligibility logged PASS |
| **8. Draft + arm + approve** | Fazal: **approve** the drafted win-back (Pillar-7) | SR drafts a win-back, arms it **L2 (awaiting_approval)**, Fazal gets the approval prompt → he approves | CC: `pending_approvals` → approved |
| **9. Send** *(the payoff)* | (automatic on approve) | The win-back **sends to +919321553267 only**, lands on the device, **exactly-once** | Fazal: message on +919321553267; CC: `send_idempotency_keys='sent'`, one outbound, no duplicate |

---

## Stop-conditions
- **No send** to any number other than +919321553267, at any step.
- Any step that fails its PASS criterion → **halt**, CC diagnoses (don't push past a red step).
- Most likely early stops: 0.6 (no lapsed-shaped data), 0.7 (no active consent → 0 candidates at Step 7), 5 (OAuth redirect/HMAC mismatch — first live run).
