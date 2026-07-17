> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# Autonomous Build Plan — 6 Concept Gaps (Fazal away, 2026-06-09)
*Grounded in `team_phase1_concept_business_plan_v1.docx`. Build on `dev` ONLY. Live customer send stays stubbed until WABA. Cowork gates risk rows with adversarial subagents; architectural rows are plan-first (Cowork reviews the plan, not Fazal). Fazal reviews all on return.*

## Operating frame
- One coherent VT row per gap-slice; CC allocates each VT-ID via `scripts/vt_id_allocate.py` at build time (the allocator drifted earlier — reconcile `.next-id` first; VT-363=cutover row must exist).
- `[BUILD]` → CC self-merge to dev on green. **Risk rows (money / customer-comms / PII / new-agent / authz) → Cowork adversarial subagent gate BEFORE the dev merge.**
- **Architectural rows (Gap 4 plan-schema, Gap 5 coordinator, Gap 6 VTR-edit) → PLAN-FIRST**: CC proposes the design, Cowork reviews via subagent, then build. Don't build the wrong shape.
- Every external-API touch (research providers, scrapers, agent LLM) gets a **real canary (Rule #15)** + a per-tenant cost line. No mock-only verification (the GSTIN/Apify lesson — 3 silent-shape bugs caught by live canaries today).
- WABA-gated sends: build the logic + tests, leave the actual send stubbed (fail-closed) until WABA.

## Build sequence (dependency-ordered)

### GAP 1 — 30-day trial, NO refund (reshape billing/trial)
- **Concept:** 30-day trial (code drifted to 14). Fazal CHANGES the concept's "recover 2x-or-auto-refund" guarantee → **pure free trial, opt-in subscribe at day 30, NO auto-charge, NO refund ever.**
- **Build:** `_TRIAL_DAYS` 14→30; reshape `transitions.py`: onboarding→trial(30d)→{subscribed | lapsed} (no card during trial; subscribe = explicit owner action at/after day 30; no auto-charge edge). **REMOVE the refund subsystem** — refund classifier (the Devanagari/Hinglish refund work), refund templates, dead-letter refund replay, refunded-tenant dispatch block, the day-25 2x-or-refund logic. decisions-ledger: supersede the refund-guarantee decision with the no-refund ruling.
- **Class:** money-path transitions → subagent gate. Independent → do FIRST.

### GAP 2 — Auto-Discovery Engine (public-source research at signup) + onboarding question-brain
- **Concept (verbatim):** *"the moment signup completes, our Auto-Discovery Engine runs across 10 external sources — Google Business Profile, [Justdial, web, social, IndiaMART, …]"* — pre-builds the business profile BEFORE onboarding asks anything. Only GBP exists today.
- **Build (2 slices):**
  - **2a — Auto-Discovery Engine:** at signup-complete, an enrichment agent fans across external sources (web-search API + company website + GBP + Justdial + social + IndiaMART…) → assembles a *draft* business_profile. **Owner-CONFIRMED, never asserted** (public data hallucinates/goes stale). Cowork picks providers (best judgment, cheap), bounds per-tenant cost, surfaces the estimate. Real canary against a known business.
  - **2b — Onboarding question-brain:** the onboarding agent reasons (per business_type + what the engine already found) about what it still needs to ask — LLM-driven, not a fixed script.
- **Class:** external API + PII + cost → subagent gate + canary. Plan-first the provider/architecture.

### GAP 3 — Guided multi-form ingestion journey (slow, part-at-a-time)
- **Concept:** "the integration barrier kills these products" → the journey must collect records in many low-effort forms over time, NOT "owner types Arun K, ₹850."
- **Build:** extend method-selector/floor into a multi-STAGE journey — a sequence of data-collection "parts" the agent drives per business type, each closed independently, paced. Wire the real launch methods: owner-assisted forms, paper-book/cash-book OCR, Google Sheets, contacts. Defer POS/UPI.
- **Class:** PII (customer records) → subagent gate.

### GAP 4 — Post-ingestion business SUMMARY + 6-month PLAN/ROADMAP (the spine)
- **Concept:** the "AI Business Partner… prioritizes actions, makes trade-offs"; the Five-Phase Roadmap.
- **Build:** PLAN-FIRST. A plan generator: once enough data (research + first ingestion), emit (a) a business summary, (b) a **6-month growth roadmap** — objectives → timeline → mapped specialist-agent actions → KPIs — stored per tenant in a defined **plan schema**. Proactive (not waiting on an owner question). Refreshes as more data lands.
- **Class:** architectural (schema is load-bearing) → Cowork reviews the schema design before build.

### GAP 5 — Autonomous specialist agents + master coordinator (Sales Recovery first), autonomy L2/L3
- **Concept:** Phase-1 product = the **Sales Recovery Agent** (finds dormant customers, drafts reactivation); the **AI Business Partner master agent** coordinates the fleet; **Autonomy Levels** — L2 = owner approves each; L3 = standard reactivation auto-sends after 20 clean approvals.
- **Build (slices):**
  - **5a — Sales Recovery Agent** (currently stubbed): dormant-customer detection (recency bands) → reactivation campaign draft → owner-confirm (L2). Self-triggers on the plan + recency. Live send STUBBED (WABA-gated).
  - **5b — Master coordinator:** runs the plan, prioritizes/schedules specialist triggers.
  - **5c — Autonomy levels:** L2 approve-each (default) → L3 auto-send after 20 clean approvals, with kill switch.
- **Class:** money/customer-comms/autonomy → heaviest gate. Plan-first the coordinator + autonomy model.

### GAP 6 — VTR edits/enhances the plan + corrects the agents
- **Build:** PLAN-FIRST. VTR surface (Ops Console, PII-gated via the existing [Resolve] door): VTR can view + edit the tenant's plan (objectives/timeline/agent params) and override/adjust an agent-proposed action before it reaches the owner. Sits on Gap-4's plan schema.
- **Class:** PII + plan-write authz (IDOR class) → subagent gate + plan-first.

## Cowork's role each cycle
Dispatch the next row → CC builds (plan-first where flagged) → Cowork adversarial subagent on risk rows → dev-merge → poll for CC's result → dispatch next. Keep polling until CC responds. Nothing to main without Fazal's promotion.
