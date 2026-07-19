# VT-231 Prod (Mumbai) Cutover — Reconciled Plan
**For:** Cowork gate + Fazal authorization  
**Produced:** 2026-06-29 (cc-winback-followups branch, 35ee3b8 — read-only recon)  
**Authority refs:** CL-422, CL-431, CL-432, Rule #18, VT-362

---

## 1. RECONCILED REALITY (Rule #14)

### 1a. VT-231 row — what's stale

The row was written 2026-05-29 and references:
- Highest migration `042_owner_feedback_surfaces.sql` (current reality: **144**)
- A secret list that predates Shopify, Razorpay billing, Sandbox GST Verify, WA Embedded Signup, VT_REF_HMAC_KEY, DBOS Conductor

Everything below is ground-truth as of this branch.

### 1b. Migration state

| Item | Reality |
|---|---|
| Migration files (total) | **145 SQL files** (`ls migrations/*.sql \| wc -l`) |
| Highest numbered | `144_vt474_business_policy.sql` |
| Next allocator | `.next-migration` = `145` |
| Init-group files | `000_init.sql`, `000a_extensions.sql`, `000b_rls_helpers.sql` (three files, applied first) |
| Gaps in sequence | **54, 109, 139** — claimed by allocator, never deployed; not missing content |
| Migration runner | `apps/team-orchestrator/scripts/apply_migrations.py` |
| Runner reads | `DATABASE_URL` → `TEAM_SUPABASE_DB_URL` (direct Postgres DSN; NOT the REST URL) |
| VT-362 guard | `--expected-env prod` required; script reads `app_environment` sentinel table and refuses to apply unless connected DB matches the declared env |
| Idempotent | Yes — re-running skips already-applied files by name |

**Migration command for prod (template — Fazal must authorize before CC executes):**
```
railway run --environment prod \
  python apps/team-orchestrator/scripts/apply_migrations.py \
  --expected-env prod
```
CC never reads the credential; Railway injects `DATABASE_URL` from the prod env → subprocess only.

### 1c. Full prod secret inventory (names only — Rule #18)

Grouped by surface. Sources: `.viabe/secrets/*.env` names, both `.env.example` files, `grep os.environ` across both apps.

#### GROUP A — Supabase / Database (prod Mumbai — Fazal provisions, values never in repo)
| Var name | Where set | Notes |
|---|---|---|
| `DATABASE_URL` | Railway Prod | Direct Postgres DSN (session-mode or direct, NOT transaction-mode pooler — see VT-505 risk below) |
| `TEAM_SUPABASE_DB_URL` | Railway Prod | Same DSN — orchestrator reads this OR `DATABASE_URL` |
| `TEAM_SUPABASE_URL` | Railway Prod | REST API URL (`https://<project>.supabase.co`) |
| `TEAM_SUPABASE_PUBLISHABLE_KEY` | Railway Prod | Anon/publishable key |
| `TEAM_SUPABASE_SECRET_KEY` | Railway Prod | Service role key — RLS bypass, highest blast radius |
| `NEXT_PUBLIC_TEAM_SUPABASE_URL` | Vercel Prod | Same REST URL, client-exposed |
| `NEXT_PUBLIC_TEAM_SUPABASE_PUBLISHABLE_KEY` | Vercel Prod | Same anon key, client-exposed |
| `SUPABASE_URL` | Vercel Prod | Alias used in team-web server routes |
| `SUPABASE_SECRET_KEY` | Vercel Prod | Alias for service role key, server-only |
| `NEXT_PUBLIC_SUPABASE_URL` | Vercel Prod | Alias used in some team-web client paths |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Vercel Prod | Alias |

#### GROUP B — Crypto / Internal secrets (NEW per env — MUST NOT reuse dev values)
| Var name | Where set | Generate via | Notes |
|---|---|---|---|
| `TEAM_PHONE_ENCRYPTION_KEY` | Railway Prod | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Fernet symmetric key — encrypts phone tokens at rest; generating a new key means existing dev tokens are unreadable (dev data is synthetic anyway per CL-422) |
| `TEAM_PHONE_HASH_SALT` | Railway Prod | `openssl rand -hex 32` | SHA-256 salt for phone tokens |
| `OPERATOR_JWT_SECRET` | Railway Prod + Vercel Prod | `openssl rand -hex 32` | Must match across orchestrator + web |
| `OWNER_JWT_SECRET` | Railway Prod | `openssl rand -hex 32` | Orchestrator-side owner session signing |
| `INTERNAL_API_SECRET` | Railway Prod + Vercel Prod | `openssl rand -hex 32` | Must match across orchestrator + web — webhook auth |
| `VT_REF_HMAC_KEY` | Railway Prod | `openssl rand -hex 32` | VTR de-identified view keying — startup fails loud if unset |
| `TEAM_ADMIN_API_TOKEN` | Railway Prod | `openssl rand -hex 32` | Ops console / admin API |

#### GROUP C — Twilio (WhatsApp + OTP)
| Var name | Where set | Notes |
|---|---|---|
| `TEAM_TWILIO_ACCOUNT_SID` | Railway Prod | Live account SID (not test) |
| `TEAM_TWILIO_AUTH_TOKEN` | Railway Prod + Vercel Prod | Live auth token |
| `TEAM_TWILIO_FROM_NUMBER` | Railway Prod | WhatsApp sender in E.164 format |
| `TWILIO_VERIFY_SERVICE_SID` | Railway Prod | Live Verify service SID |

#### GROUP D — Razorpay (billing)
| Var name | Where set | Notes |
|---|---|---|
| `TEAM_RAZORPAY_KEY_ID` | Railway Prod | Live `rzp_live_*` key |
| `TEAM_RAZORPAY_KEY_SECRET` | Railway Prod | Live key secret — NEVER echoed |
| `TEAM_RAZORPAY_WEBHOOK_SECRET` | Railway Prod | Razorpay dashboard generates per-webhook endpoint — NEW for prod URL |
| `RAZORPAY_WEBHOOK_SECRET` | Vercel Prod | Same webhook secret — web handler |
| `NEXT_PUBLIC_RAZORPAY_KEY_ID` | Vercel Prod | Live publishable key_id (safe to expose to client) |
| `TEAM_RAZORPAY_LIVE` | Railway Prod | `true` in prod (boolean flag) |

#### GROUP E — Sandbox by Quicko (GST LIVE verification)
| Var name | Where set | Notes |
|---|---|---|
| `SANDBOX_API_KEY` | Railway Prod | LIVE key (not test) — `.viabe/secrets/sandbox.env` has both; use `sandbox_api_key` for prod (maps to `SANDBOX_API_KEY` on Railway) |
| `SANDBOX_API_SECRET` | Railway Prod | LIVE secret |
| `SANDBOX_BASE_URL` | Railway Prod | Production Sandbox endpoint (no test-mode URL) |
| `TEAM_SANDBOX_GST_MOCK_MODE` | Railway Prod | `false` in prod (must not be `true`) |

**Risk:** Sandbox LIVE keys require a funded wallet. Verify wallet balance before first real customer onboard.

#### GROUP F — Anthropic / LLM
| Var name | Where set | Notes |
|---|---|---|
| `TEAM_ANTHROPIC_API_KEY` | Railway Prod | Production key (may be same account, different usage tier) |
| `ANTHROPIC_BASE_URL` | Railway Prod | Absent = default; set only if using a proxy |
| `VIABE_CANARY_MODEL` | Railway Prod | Model override for canary runs |

#### GROUP G — ScrapingBee
| Var name | Where set | Notes |
|---|---|---|
| `SCRAPINGBEE_API_KEY` | Railway Prod | May share dev key; check rate limits |

#### GROUP H — Apify
| Var name | Where set | Notes |
|---|---|---|
| `APIFY_API_TOKEN` | Railway Prod | May share dev token; check quota |

#### GROUP I — Voyage (vectors)
| Var name | Where set | Notes |
|---|---|---|
| `VOYAGE_API_KEY` | Railway Prod | Embedding model API |

#### GROUP J — Resend (transactional email)
| Var name | Where set | Notes |
|---|---|---|
| `TEAM_RESEND_API_KEY` | Railway Prod | Sending-only key (Resend read endpoints 401 on this key type) |
| `RESEND_FROM_EMAIL` | Railway Prod | Verified sender address |
| `RESEND_TO_EMAIL` | Railway Prod | Ops alert target |

#### GROUP K — Shopify (integration connector)
| Var name | Where set | Notes |
|---|---|---|
| `SHOPIFY_API_KEY` | Railway Prod | Shopify Partner app key |
| `SHOPIFY_API_SECRET` | Railway Prod | Shopify app secret |
| `SHOPIFY_WEBHOOK_SECRET` | Railway Prod | Shopify generates per-webhook; NEW for prod URL |
| `SHOPIFY_OAUTH_REDIRECT_URI` | Railway Prod | Prod callback URL |

#### GROUP L — WhatsApp / Meta Embedded Signup
| Var name | Where set | Notes |
|---|---|---|
| `WA_APP_ID` | Railway Prod | Meta app ID for WABA Embedded Signup |
| `WA_APP_SECRET` | Railway Prod | Meta app secret |
| `WA_CONFIG_ID` | Railway Prod | Embedded Signup config ID |
| `WA_REDIRECT_URI` | Railway Prod | Prod OAuth callback URL |

#### GROUP M — Google OAuth
| Var name | Where set | Notes |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Railway Prod | Google Cloud console OAuth client |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Railway Prod | Google OAuth secret |
| `GOOGLE_OAUTH_REDIRECT_URI` | Railway Prod | Prod callback URL |

#### GROUP N — DBOS
| Var name | Where set | Notes |
|---|---|---|
| `DBOS_APPLICATION_NAME` | Railway Prod | Must match name registered on console.dbos.dev |
| `DBOS_CONDUCTOR_KEY` | Railway Prod | Optional — absent = local-recovery mode only |

#### GROUP O — Telegram (Ops alerts)
| Var name | Where set | Notes |
|---|---|---|
| `TELEGRAM_OPS_BOT_TOKEN` | Railway Prod + Vercel Prod | Production Ops bot |
| `TELEGRAM_OPS_CHAT_ID` | Railway Prod | Ops channel ID |
| `TELEGRAM_OPS_WEBHOOK_SECRET` | Vercel Prod | Webhook validation secret — NEW for prod |

#### GROUP P — Observability (LangSmith / Logfire)
| Var name | Where set | Notes |
|---|---|---|
| `LANGSMITH_API_KEY` | Railway Prod | Set to `viabe-team-prod` project; absent = tracing disabled (non-fatal) |
| `LANGSMITH_PROJECT` | Railway Prod | `viabe-team-prod` |
| `LANGCHAIN_TRACING_V2` | Railway Prod | `true` |
| `LANGCHAIN_PROJECT` | Railway Prod | `viabe-team-prod` |
| `LOGFIRE_TOKEN` | Railway Prod | Absent = Logfire disabled (non-fatal) |
| `LOGFIRE_PROJECT` | Railway Prod | Prod project name |

#### GROUP Q — Runtime config (non-secret, prod-specific values)
| Var name | Prod value | Notes |
|---|---|---|
| `EXPECTED_ENV` | `prod` | VT-362 guard sentinel — triggers all boot assertions |
| `TEAM_TWILIO_MOCK_MODE` | absent / `false` | Mock sends must be OFF in prod |
| `TEAM_TWILIO_VERIFY_MOCK_MODE` | absent / `false` | VT-434: boot fails if `true` + `EXPECTED_ENV=prod` |
| `MARKETING_CONSENT_VERSIONS` | **empty** until counsel sign-off | C2 prod-boot guard — stays empty until Fazal + counsel activate |
| `TEAM_RAZORPAY_LIVE` | `true` | Activates live billing path |
| `STANDARD_PRICE_PAISE` | `499900` | ₹4,999 |
| `NEXT_PUBLIC_OFFERED_TIERS` | `standard` | Single-plan launch gate (VT-429) |
| `NEXT_PUBLIC_SITE_URL` | `https://viabe.ai/team` (or prod domain) | |
| `TEAM_TWILIO_WEBHOOK_URL` | Prod Vercel webhook URL | Twilio must be re-pointed to prod |
| `TEAM_ORCHESTRATOR_URL` | Railway prod orchestrator URL | |
| `FAZAL_OWNER_UUID` | Re-derived from prod Supabase Auth | After creating the RKeCom tenant on prod |
| `FAZAL_TENANT_ID` | Re-derived from prod tenants table | After inserting RKeCom tenant on prod |
| `OWNER_PORTAL_URL` | Prod Vercel URL | Orchestrator link-out base |
| `HOOK_BASE_URL` | Prod orchestrator base URL | Webhook routing |

#### Summary: NEW-PER-ENV (must be freshly generated — NEVER copy from dev)
`TEAM_PHONE_ENCRYPTION_KEY`, `TEAM_PHONE_HASH_SALT`, `OPERATOR_JWT_SECRET`, `OWNER_JWT_SECRET`, `INTERNAL_API_SECRET`, `VT_REF_HMAC_KEY`, `TEAM_ADMIN_API_TOKEN`, `TEAM_RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `SHOPIFY_WEBHOOK_SECRET`, `TELEGRAM_OPS_WEBHOOK_SECRET`

---

## 2. PHASED PLAN

Every step tagged **[Fazal]** requires Fazal's direct action or explicit authorization before CC proceeds. CL-431 applies to every prod env-var and migration change.

### PHASE 0 — Prerequisites (Fazal-side; blocks everything)

**[Fazal] Step 0.1 — Provision Supabase prod Mumbai**
- Create a new Supabase project: name `viabe-team-prod`, region `ap-south-1` (Mumbai) on a paid tier (paid tier required for region selection; free tier defaults to a non-Mumbai region per CL-422).
- Backup preference: `ap-south-2` (Hyderabad) if Mumbai is unavailable.
- Enable point-in-time recovery (launch requirement; per CL-422/ADR-0003).
- Save the project URL, anon key, service role key, and **direct database connection string** (NOT the pgBouncer pooler URL — see VT-505 risk; use the direct connection from Supabase project settings → Database → Connection string → URI, host `db.<project>.supabase.co:5432`).

**[Fazal] Step 0.2 — Generate ALL prod secrets (NEW, never copy from dev)**
For every var in Group B (crypto) and Group C–O (vendor secrets):
- Generate new crypto values using the commands in Group B above.
- Collect vendor prod credentials (Twilio live, Razorpay live, Sandbox live, Shopify prod webhook, etc.).
- Store in `.viabe/secrets/supabase-prod.env` and companion files (gitignored). Do not commit values.

**[Fazal] Step 0.3 — Set Railway Prod env vars**
- In Railway console, under the `prod` environment: set ALL vars from Groups A–Q above.
- Critically: `EXPECTED_ENV=prod`, `TEAM_SUPABASE_DB_URL` pointing at the direct Postgres DSN (not pooler), `DATABASE_URL` set identically.
- `MARKETING_CONSENT_VERSIONS` = **empty string** (or absent) — do NOT copy dev's `winback_optin_v1_dev_2026-06` value.
- `TEAM_TWILIO_VERIFY_MOCK_MODE` must be absent or `false` (VT-434 boot assertion will fail-closed if it's `true` with `EXPECTED_ENV=prod`, but that assertion isn't built yet — see open items).

### PHASE 1 — Schema (CC executes, Fazal-authorized per CL-431)

**[Fazal-authorized] Step 1.1 — Run all migrations against prod**

Authorization precondition: Fazal explicitly grants this step ("apply migrations to prod").

CC executes via by-reference injection (CC never reads the DSN plaintext):
```
railway run --environment prod \
  python apps/team-orchestrator/scripts/apply_migrations.py \
  --expected-env prod
```

Expected outcome: 145 migrations applied (000–144, skipping the 3 gaps at 54/109/139 which are absent from disk). Script reports the count and exits 0. CC reports the schema_migrations count + confirms zero failures — never the DSN value.

**[CC] Step 1.2 — Verify migration count**
Query (via `railway run --environment prod python -c "import psycopg, os; conn = psycopg.connect(os.environ['DATABASE_URL']); row = conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone(); print('Applied migrations:', row[0])"`) — report count only, no schema contents with PII.

### PHASE 2 — Infra wiring (Fazal-side)

**[Fazal] Step 2.1 — Create Vercel `viabe-team-web` prod project**
- In Vercel dashboard: create project named `viabe-team-web` (distinct from `viabe-team-web-dev`).
- Connect to `rkecom-in/viabe-team` repo, branch `main`.
- Configure auto-deploy: ON for `main` pushes. "Wait for CI": decision for Fazal (production-grade may want this OFF initially for direct control).
- Set ALL Vercel Prod env vars from Groups A (public keys only), C (auth token), D (Razorpay), H (internal), O (Telegram), Q (config).
- Note: Vercel prod project naming vs Supabase naming: Supabase project = `viabe-team-prod` (separate service), Vercel project = `viabe-team-web` — do not conflate.

**[Fazal] Step 2.2 — Wire Railway Prod environment to `main` branch**
- Confirm Railway → Prod environment → branch binding = `main` (Railway console setting, not CLI-inspectable per CLAUDE.md topology note).
- Confirm "Auto deploys on push" = ON; "Wait for CI" = ON (so `deploy-dev.yml` green gates the orchestrator deploy, same pattern as dev).

**[Fazal] Step 2.3 — Re-point Twilio webhook to prod URL**
- In Twilio console: update the inbound WhatsApp webhook URL to the prod Vercel URL (`https://<prod-domain>/api/team/twilio/webhook`).
- Update Razorpay webhook endpoint to prod URL.
- Update Shopify webhook endpoint to prod URL (per-tenant, handled at OAuth time, but verify the platform app config).

**[Fazal] Step 2.4 — Create RKeCom prod tenant + derive runtime IDs**
After migrations applied and Railway prod is running:
- Create the RKeCom self-tenant row in the prod `tenants` table via the admin API or direct Supabase console insert.
- Derive `FAZAL_OWNER_UUID` from Supabase Auth on prod (sign up or create the auth user for Fazal's prod account).
- Derive `FAZAL_TENANT_ID` from the inserted tenants row.
- Set both in Railway Prod + Vercel Prod env vars.

### PHASE 3 — Smoke + residency canary (CC executes, Fazal-authorized)

**[Fazal-authorized] Step 3.1 — VT-169 residency canary against prod**
Run the Supabase region assertion: confirm the prod pooler/DB host resolves to `ap-south-1` (Mumbai). This is the assertion VT-169 ran for dev and confirmed Seoul. For prod, expectation is `aws_region_for_ip == "ap-south-1"` or equivalent Mumbai region marker.

Command (read-only network check, no data write):
```
railway run --environment prod \
  python -c "
import socket, os
url = os.environ.get('TEAM_SUPABASE_URL','')
host = url.replace('https://','').split('.')[0] + '.supabase.co'
print('Host:', host)
import socket; addr = socket.gethostbyname(host); print('IP:', addr)
"
```
Report the resolved IP only. Fazal or CC cross-checks it against ap-south-1 AWS IP ranges (or checks the Supabase dashboard directly). A Seoul IP on prod = STOP, investigate provisioning.

**[CC] Step 3.2 — Prod boot smoke**
After Railway prod deploys (triggered by dev→main promotion), hit the orchestrator health endpoint and confirm `{"expected_env": "prod", "status": "ok"}`. Do not send any messages or touch customer paths.

### PHASE 4 — dev→main promotion (Fazal-authorized, CL-432)

**[Fazal-authorized] Step 4.1 — Authorize the dev→main promotion PR**
Per CL-432: CC NEVER merges to `main` without an explicit Fazal `type: task` promotion instruction. Cowork relays the signal. When Fazal issues the authorization:
- CC opens a `dev→main` PR.
- PR must be clean (no pending VT rows that need to land first).
- CI green on the PR.
- Fazal or CC merges after explicit authorization.

Note: `main` merge triggers Railway prod native auto-deploy (branch binding confirmed in Step 2.2). No `railway up` in CI — Railway's "Wait for CI" gates on `deploy-dev.yml` green.

### PHASE 5 — Launch gates (must clear BEFORE first real customer)

These are NOT VT-231 acceptance, but VT-231 CANNOT be called done if any of these are unresolved:

| Gate | Row | Owner | Status |
|---|---|---|---|
| Single-plan launch gate (offer STANDARD only server-side) | VT-429 | CC (Cowork gate — money path) | Queued |
| Prod-guard: boot assertion against `TEAM_TWILIO_VERIFY_MOCK_MODE` in prod | VT-434 | CC (Cowork gate — auth) | Queued |
| DPDP breach notification (`notify_customer` + `notify_dpdpa_authority` unbuilt) | VT-437 | CC + Fazal/counsel | Queued |
| Pre-prod copy review (owner-facing draft/placeholder copy) | VT-438 | Fazal (copy approval) | Queued |
| Counsel package (C1–C3: privacy notice, owner_inputs framing, legal copy) | VT-156 | Fazal + counsel | In flight |
| `MARKETING_CONSENT_VERSIONS` prod activation | config | Fazal + counsel | Blocked on counsel |

### PHASE 6 — Decisions ledger update (CC)

After VT-231 acceptance, CC files a CL entry and updates `docs/clau/decisions-ledger.md`:
```
VT-231 done <YYYY-MM-DD>; prod residency verified ap-south-1 (Mumbai); 
144 migrations applied; Vercel viabe-team-web created; Railway prod wired to main.
```

---

## 3. DISCIPLINE

These are not reminders — they are binding constraints on every step above:

**CL-422 (STANDING, launch-gate sunset):** NO real beta-partner customer data (phone numbers, ledger rows, WhatsApp message bodies) enters the prod Supabase database until VT-231 is closed AND VT-231 is verified. Even after VT-231 closes, NO real customer data until the Phase 5 launch gates clear.

**CL-431 (STANDING):** Every prod env-var change — config or secrets — requires explicit Fazal authorization first. A dev grant (e.g., "set X on dev") never implies prod authority. CC manages dev env vars autonomously; prod is Fazal-authorized per change or per phase.

**CL-431 secrets hygiene (binding):** CC NEVER writes a live secret VALUE into any repo file, signal, log, PR, or commit. CC sets in Railway/Vercel console directly and reports only the variable NAME + action ("set `VAR_NAME` in prod Railway"). By-reference injection for any process that must consume a prod secret at runtime (`railway run --environment prod ...`).

**Rule #18 (env inspection is booleans only):** CC NEVER runs `railway variables` or `railway variables --json`. All env presence checks via `scripts/env_presence.py`. Live values never enter CC's context.

**EXPECTED_ENV=prod fail-closed guard (VT-362):** `apply_migrations.py` refuses to apply unless the connected DB's `app_environment` sentinel table matches `--expected-env prod`. This structurally prevents a dev-DSN/prod-env mismatch from silently writing to the wrong database.

**Dev=Seoul / Prod=Mumbai residency (ADR-0003 / CL-422):** The dev Supabase project is permanently in `ap-northeast-2` (Seoul). This is the accepted deviation. Prod MUST be in `ap-south-1`. Never accept a prod project that resolves to Seoul.

**`main` is Fazal-authorized ONLY (CL-432 / Pillar 7):** CC never merges to `main` without a `type: task` authorization signal from Fazal. No exceptions.

**No seed/synthetic data against prod:** The migration runner guard (`--expected-env prod`) rejects seed scripts. Do not run test fixtures or synthetic-data inserts against the prod DB, ever.

---

## 4. OPEN ITEMS AND RISKS

### BLOCKER-CLASS (must resolve before VT-231 can close)

**[INFRA RISK — VT-505] DBOS broken on dev, likely to recur on prod if same pooler URL used**

VT-505 confirms `dbos.workflow_status` is absent on the dev Supabase DB — DBOS's workflow runtime is not fully initialized. Diagnosed probable cause: `DATABASE_URL` / `TEAM_SUPABASE_DB_URL` on Railway dev points at the **transaction-mode pgBouncer pooler** (Supabase default; `db.<project>.supabase.co:6543`). DBOS requires persistent session-level connections for its workflow-state tables; pgBouncer in transaction mode does not support session-scoped features. Only `transaction_outputs` (a simple table insert) works; `workflow_status` (requires session semantics or a direct connection) does not initialize.

**Implication for prod:** If `TEAM_SUPABASE_DB_URL` on Railway Prod is set to the pooler URL (the default that Supabase copies to your clipboard), the same DBOS initialization failure will occur on prod. `l2_send_workflow`, `l3_hold`, the autonomous coordinator cron, and all scheduled sweeps will silently fail.

**Fix (decide before Step 0.3):** Fazal must use the **direct database connection string** (`postgresql://postgres:<password>@db.<project>.supabase.co:5432/postgres`) — NOT the pooler URL (`aws-0-ap-south-1.pooler.supabase.co:6543`) — for `DATABASE_URL` and `TEAM_SUPABASE_DB_URL` on Railway Prod. The Supabase dashboard shows both; choose "Direct connection" not "Transaction" or "Session" pooler. VT-505 should be fixed on dev with the same change before this is validated.

**[NOT YET BUILT — VT-434] Prod OTP-guard not implemented**

The boot assertion that prevents `TEAM_TWILIO_VERIFY_MOCK_MODE=true` from running in prod is in the VT-434 row (Queued). Until this is built and merged, there is no structural guard — it relies on the operator (Fazal/CC) correctly setting the Railway Prod env var to absent/false. Recommend building VT-434 before the dev→main promotion.

**[NOT YET CREATED] Vercel `viabe-team-web` prod project does not exist**

Per CLAUDE.md two-environment topology: `viabe-team-web-dev` exists; `viabe-team-web` (prod) does NOT yet exist. Creation is part of VT-231 scope. This is a Fazal action (Step 2.1 above).

**[VENDOR-SIDE] Sandbox LIVE keys and wallet balance**

The dev secrets file (`sandbox.env`) has both test and live keys: `sandbox_api_key` (live) and `sandbox_api_key_test`. Prod must use the LIVE keys. Sandbox operates on a PAYG wallet — verify the wallet has sufficient balance before the first real customer GST verification. A zero-balance wallet causes verification to fail closed (status stays `unverified`), which is safe but blocks onboarding.

### HIGH-RISK (must clear before first real customer send)

**VT-429 (Queued — money gate):** Server-side offered-tiers allowlist not yet built. Without it, a client could request a non-STANDARD plan. Build before any real customer sees the subscribe surface.

**VT-437 (Queued — DPDP):** `notify_customer` and `notify_dpdpa_authority` are unbuilt stubs in `breach_notification.py`. Legally required under DPDP before customer data enters prod. Fazal + counsel must decide notification obligations + copy; CC builds the mechanism.

**VT-438 (Queued — copy):** Several owner-facing messages still carry draft/placeholder copy (`_CONSENT_PROMPT` marked INTERIM, Hindi i18n first-pass). Fazal must approve all live-sent copy before first real customer send.

**Counsel C1–C3 package:** Privacy notice + owner_inputs framing + public legal copy — Fazal + counsel still in flight. `MARKETING_CONSENT_VERSIONS` on prod stays empty until this clears.

### LOWER RISK (flag, not blocking)

**WA Embedded Signup live walk (VT-286 deferred):** The WABA OAuth round-trip is built and unit-tested but the live Meta ES walk is E2E-deferred. Trademark filing status gates the WABA display name. No owner-owned WABA can be provisioned until this walk completes. Prod can exist without it — the flow is fail-closed if WABA status is not `live`.

**Shopify OAuth live merchant walk (VT-283 deferred):** Same pattern — built + tested, live walk requires a real merchant store on a different org. Deferred to E2E.

**Razorpay webhook URL re-pointing (Step 2.3):** The prod Razorpay webhook endpoint URL must be updated to the Vercel prod URL. If this is missed, `payment.captured` events go to dev and are silently dropped on dev (or cause duplicate processing if dev is also running). Easy to miss during go-live.

**LangSmith project drift:** `LANGSMITH_PROJECT` defaults to `viabe-team-dev` in `.env.example`. On Railway Prod, explicitly set it to `viabe-team-prod` — otherwise prod traces contaminate the dev project.

**`TEAM_MONTHLY_REPORT_BUCKET` not in any current secrets file:** Referenced in orchestrator code but absent from `.viabe/secrets/`. If monthly report storage is in scope at launch, Fazal must provision a storage bucket and set this var on Railway Prod.

**Migration gap at 139 (between `138_vt405_...` and `140_vt420_...`):** Number 139 was allocated but no SQL file exists. The migration runner handles this gracefully (only applies files present on disk). Not a correctness issue, but worth noting so a future engineer doesn't assume a missing migration in the schema_migrations log is a corruption.

---

## VT-231 Acceptance (updated from stale row)

- [ ] Supabase prod project exists in `ap-south-1` (Mumbai), verified via dashboard
- [ ] All 145 migration files applied (`schema_migrations` count matches, zero errors)
- [ ] `EXPECTED_ENV=prod` set on Railway Prod; VT-362 guard confirmed at boot
- [ ] `DATABASE_URL` / `TEAM_SUPABASE_DB_URL` uses direct Postgres connection (NOT pooler) — VT-505 risk mitigated
- [ ] Vercel `viabe-team-web` prod project created, wired to `main`
- [ ] All Group B NEW-per-env secrets generated fresh (not copied from dev)
- [ ] VT-169 canary against prod returns `ap-south-1` (not Seoul)
- [ ] Prod boot smoke: orchestrator health endpoint returns `{"expected_env":"prod"}`
- [ ] `decisions-ledger.md` updated with VT-231 done date + residency confirmation
- [ ] Pre-launch gates (VT-429, VT-434, VT-437, VT-438, counsel) tracked separately — VT-231 close does NOT mean customer sends are authorized; it means the substrate is ready

---

*This document is a Cowork gate artifact and Fazal authorization brief. CC produced it read-only; no code, DB, env, or git changes were made in this session beyond writing this file.*


> **CORRECTION (2026-06-30, VT-505 resolved):** The 'DBOS pooler init failure' risk flagged here was a MISDIAGNOSIS — DBOS works fine on the session-mode pooler; workflow_status lives in the separate `postgres_dbos_sys` DB (not the app `postgres` DB). The real bug was a contained code recursion in l3_hold.py (VT-505, fixed). Prod does NOT need a pooler-vs-direct change FOR DBOS. (apply_migrations.py still needs a direct DSN for its own schema migrations — unrelated to DBOS.)
