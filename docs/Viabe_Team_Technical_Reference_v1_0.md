**Doc status:** v1.0 · 2026-05-29
**Reflects code state as of:** main @ 46da1d9
**Next refresh trigger:** when Sprint 3 starts OR a Standing decision invalidates a section

---

# Viabe Team — Technical Reference v1.0

This is a briefing and index. Architecture decisions live in `docs/adr/`; operational procedures live in `docs/runbooks/`; Standing decisions live in `docs/clau/decisions-ledger.md`. This doc shows where the pieces fit, not what's inside each piece.

---

## 1. Executive Overview

**Viabe Team** is a multi-agent system for small Indian business owners (Tier-2/3 SMB). WhatsApp-first. Owner-facing portal at `viabe.ai/team`. Three deployable apps in a Python + Next.js monorepo:

- `apps/team-orchestrator/` — Python 3.13, DBOS + LangGraph + Anthropic SDK; the critical path
- `apps/team-web/` — Next.js 16, React 19; webhook handlers + onboarding + dashboard + Ops Console
- `apps/team-ingestion-worker/` — Python 3.13; planned Apify + Sarvam ingestion (currently a SystemExit stub)

Binding launch milestone: **Reports-Jun15 (2026-06-15)**. Sprints 1 + 2 ship for that gate. Everything else is ship-thin.

Target persona: SMB owner (salon, restaurant, retail). Not a developer. Every customer-facing surface is constrained by CL-421 (zero-manual-paste connectors) and CL-330 (owner_inputs = structured intent, not raw bodies).

## 2. Architecture at a Glance

Three sibling GitHub repositories (`rkecom-in/`):

- `viabe-team` — this repo
- `viabe-reports` — analytic reports product
- `viabe-marketing` — marketing site

All served under `viabe.ai` via Vercel path-based rewrites: `viabe.ai/team/*` → team-web; `viabe.ai/report/*` → reports; `viabe.ai/*` → marketing. See **ADR-0005** (three sibling repos) + **ADR-0006** (path routing) + `docs/clau/deployment-shape.md` for the full topology.

Substrate:

- **Supabase Postgres** — sole stateful store (workflow state, business data, observability, L0 memory, KG vectors via pgvector, Realtime substrate). Two projects: dev (ap-south-1 Mumbai, pending VT-169 verification) + prod (ap-south-2 Hyderabad, to-provision). See **ADR-0003**.
- **Railway** — orchestrator host. One service per environment. See `docs/clau/dev-env-runbook.md` and **ADR-0001** (DBOS substrate).
- **Vercel** — team-web host. Edge in `bom1` (Mumbai). PR previews enabled per VT-223.

## 3. Orchestrator + LangGraph supervisor

LangGraph supervises the outer multi-agent dispatch (`supervisor.py` + `graph.py`). Inside each agent node, the inner tool-use loop runs via `create_agent` (LangChain over Anthropic SDK) with `cache_control` blocks on system prompts + tool registry. See **ADR-0002** (LangGraph + Agent SDK split).

Agents registered today:

- **SR-Agent** (`sales_recovery.py`) — sales recovery; produces `CampaignPlan` envelope; out_of_scope returns route to other specialists
- **Integration Agent** (`integration_agent.py`) — connector onboarding + ingestion routing
- **Orchestrator Agent** (`orchestrator_agent.py`) — top-level supervisor

DBOS workflows (per `apps/team-orchestrator/src/main.py` lifespan; register-before-launch contract per CL-220):

- `register_purge_scheduler` (VT-200) — retention purge
- `register_scheduled_triggers` (VT-28) — 4 scheduled trigger workflows
- `register_ingestion_scheduler` (VT-210, VT-215) — 5-min ingestion fan-out
- `register_alert_scheduler` (VT-202) — proactive alerts + daily digest
- `register_drive_push_scheduler` (VT-222) — Drive Push delta + 6h renewal + 10min polling fallback

Workflow + scheduled pattern: `DBOS.workflow()(fn)` FIRST, then `DBOS.scheduled(cron)(fn)`. Closing this gap is what VT-215 retroactively did for ingestion_scheduler_body.

## 4. Integration Agent + Connector Discipline (CL-421)

**Every connector MUST be zero-manual-paste after OAuth.** No Apps Script paste, no copy-paste secrets, no developer-shaped setup steps. OAuth grant + auto-configure via vendor API is the only acceptable customer-facing flow. See **ADR-0004** and `docs/clau/sheet-integration-runbook.md`.

Shipped connectors:

- **Google Sheet** (VT-207 substrate + VT-222 redesign) — OAuth → Drive Push channel auto-registered → real-time push on edits + 10-min polling fallback for unwatched/stale tenants. Legacy `setup_push` / `apps_script_template` deprecated, kept for backward compat with pre-VT-222 tenants.
- **Shopify** (VT-208) — OAuth → Admin API auto-subscribes webhook topics → push delivery on order events.

Per-tenant ingestion state lives in `tenant_connector_status` + `tenant_oauth_tokens` + `tenant_drive_channels`. Field-mapping via VT-209 (LLM-mapped vendor columns → internal customer schema). Phone-hash dedupe via VT-184 (`phone_tok_<sha256[:16]>`).

## 5. Observability + Cost

Two parallel substrates writing to the same `pipeline_steps` schema (per CL-417 canonical schema):

- **Outer loop** (LangGraph supervisor) — `OrchestratorReasoningCallback` (VT-125) emits one row per supervisor step + per-agent dispatch; captures tokens_input/output, cost_paise, model_used
- **Inner loop** (Anthropic SDK tool use) — `@tool_step` decorator (VT-181) wraps every MCP tool + tool function; ContextVar (`_observability_context`) propagates run_id/tenant_id

Cost tracking: VT-194 prompt caching wired via `cache_control: {type: ephemeral}` on system prompts + tool registry. Observed ~6.9x cost reduction on system prompts.

Logfire/OTel ships spans to Pydantic-hosted Logfire when `LOGFIRE_TOKEN` is set. Substrate-side observability (pipeline_runs/steps) is the load-bearing customer-facing view; Logfire is for debug.

Free-text search at scale via tsvector + GIN index on `pipeline_steps.envelope_search_tsv` (VT-216 — replaces VT-201 PR-2 ILIKE fallback).

## 6. Privacy + Residency

CL-390 cluster + CL-104 + CL-330 + CL-416:

- **Phone-at-rest encryption** — Fernet via `TEAM_PHONE_ENCRYPTION_KEY`. Key lives in orchestrator process only (defense-in-depth; never on web tier).
- **Phone-hash for cross-table joins** — `phone_tok_<sha256[:16]>` (VT-184). Allows dedupe without storing E.164 in foreign tables.
- **Per-tenant RLS** — every business table policy is `tenant_id = app_current_tenant()`. Operator-claim JWT (VT-188, ADR-0008) bypasses tenant RLS for Ops Console reads only.
- **Lifetime-of-relationship retention** (CL-416) — `pipeline_runs`, `pipeline_steps`, `phone_token_resolutions` have no time-based deletion; DSR-purge is the sole deletion path. Privacy notice (VT-156) discloses this.
- **DPDP residency** — dev project pooler hostname currently resolves to `aws-1-ap-northeast-2`. **VT-169 canary will confirm Interpretation 3 (pooler topology — primary DB sits in India) vs Interpretation 1 (real misconfig).** If real misconfig surfaces, residency migration is filed as a follow-up VT. Privacy notice + final legal review (VT-115) gate on VT-169 outcome.

PII scrub helper (VT-202 substrate `alerts/pii_scrub.py`) MUST be applied to every external-facing surface (alerts, audit logs, customer comms). Pattern: E.164 phones + ≥7-digit runs + Twilio SIDs → `[REDACTED]`. PII redaction failure recovery is `docs/runbooks/pii-redaction-failure.md`.

## 7. Ops Console + Operator Auth

`/team/ops/*` routes gated by `requireFazal()` (HS256 JWT in HttpOnly cookie scoped to that path). Sub-surfaces:

- `/team/ops` — workspace overview (counters, top tenants, in-flight runs). Per-fetch try/catch resilience per VT-217.
- `/team/ops/stream` — Supabase Realtime live pipeline_steps with per-tenant filters + banner aggregation (VT-201 PR-1)
- `/team/ops/stream/history` — keyset-paginated history view with tsvector free-text search (VT-201 PR-2 + VT-216)
- `/team/ops/runs/[runId]` — single-run waterfall (VT-201 PR-3)

Two auth substrates per **ADR-0008**:

- **operator-JWT** — Fazal session via magic-link Supabase Auth at `/team/ops/login` (VT-203). HS256 with `OPERATOR_JWT_SECRET`. 1-hour TTL.
- **admin-token** — `TEAM_ADMIN_API_TOKEN` (32-byte hex) on `X-Team-Admin-Token` header for `/api/orchestrator/admin/*` (VT-224). In-process 10 req/sec rate limit per token. Every call writes `admin_audit_log` with 8-char sha256 fingerprint.

Banner severity (green / yellow / red) aggregated from VT-202 alert substrate + recent escalations + hard-limit hits.

## 8. Memory: L0 in-house, L1 deferred

Per **ADR-0009**:

- **L0** — VT-126 substrate. Cohort-keyed fragments in Postgres + JSONB payloads. k-anonymity admission gate (k=10 per CL-28). Consent gate per CL-390 (only tenants with `owner_inputs.enabled = true` contribute).
- **L1** — semantic / KG layer. **Gated on `docs/clau/l1-tenant-context-design.md` (Cowork-drafted, Fazal-reviewed) per VT-195 brief. L1 in-house implementation does not begin until concept doc lands.** Substrate decision (in-house pgvector + relational vs Mem0 SaaS) will get its own ADR once VT-195 lands.

L0 production write wiring is VT-196 — adds a post-step writer through the k-anonymity gate. Async (DBOS workflow); never blocks the agent response path.

## 9. Sprint plan + launch milestones

| Sprint | Anchor | Status | Reports-Jun15 ship? |
|---|---|---|---|
| Sprint 1 | Foundation (Postgres, Twilio, DBOS, observability) | Shipped | ✓ |
| Sprint 2 | Integration Agent (per ADR-0007 re-anchor) | In progress; Drive Push (VT-222) + admin endpoints (VT-224) + Sheet substrate landed; SR-Agent (VT-4) + MCP tools (VT-40..49) deferred to Sprint 2.5/3 | ✓ for Integration |
| Sprint 3+ | Ingestion methods + KG | Planned | Concierge-bridged for Reports-Jun15 |
| Sprint 7 | L1 KG | Planned; concept doc gate | Out of scope for Reports-Jun15 |
| Sprint 8 | Launch cluster (Razorpay Live, Twilio DLT, product copy) | Fazal/vendor blocked | Gates final launch |
| Sprint 9 | Polish + E2E + tabletop drills | Planned | ✓ pre-launch |

For Reports-Jun15: SR-Agent specialist agents ship as stubs; Fazal-led concierge phase covers the gap. Once VT-4 + MCP tools land, agent automation replaces concierge per tenant cohort.

## 10. Reference catalogue

### Architecture decision records (`docs/adr/`)

| ADR | Title |
|---|---|
| ADR-0001 | DBOS substrate (vs Temporal) |
| ADR-0002 | LangGraph orchestrator + Agent SDK split |
| ADR-0003 | Supabase Postgres single-substrate |
| ADR-0004 | Zero-manual-paste connectors (Apps Script abandoned) |
| ADR-0005 | Three sibling repos |
| ADR-0006 | Path-based routing under viabe.ai |
| ADR-0007 | Sprint 2 re-anchored to Integration Agent |
| ADR-0008 | Operator-JWT vs admin-token split |
| ADR-0009 | Memory tiering — L0 in-house, L1 deferred |

### Operational runbooks (`docs/runbooks/`)

Twilio webhook outage · Supabase region failover · DBOS workflow stuck · Anthropic rate limit · Razorpay sig mismatch · Apify actor failure · Drive Push channel renewal failure · Logfire outage · DSR export workflow · PII redaction failure · Operator JWT compromise.

### Clau-internal runbooks (`docs/clau/`)

`admin-endpoints-runbook.md` (VT-224) · `sheet-integration-runbook.md` (VT-222) · `dev-env-runbook.md` · `region-verify-runbook.md` (VT-169) · `deployment-shape.md` (VT-120).

### Standing decisions (`docs/clau/decisions-ledger.md`)

Load-bearing: CL-79 (Postgres substrate) · CL-36 (DBOS) · CL-29 (LangGraph + Agent SDK) · CL-41 (three repos) · CL-132 (path routing) · CL-220 (operator-JWT) · CL-330 (owner_inputs structured intent) · CL-390 (privacy cluster) · CL-416 (lifetime retention) · CL-417 (canonical schema) · CL-418 (Rule #17 git stash) · CL-421 (zero-paste connectors).

### Sprint substrate (`.viabe/sprint/VT-*.md`)

Numeric-only IDs (CL-discipline). Allocator: `scripts/vt_id_allocate.py`. Sprint board state is `.viabe/sprint/VT-<N>.md` per row.

### Key migrations (`migrations/*.sql`)

| File | Purpose |
|---|---|
| 006_pipeline_steps.sql | Pipeline observability core |
| 025_pipeline_observability_normalize.sql | Canonical schema per CL-417 |
| 030_realtime_streams.sql | Ops Console Realtime substrate |
| 033_tenant_oauth_tokens.sql | Connector OAuth substrate |
| 037_tenant_alert_substrate.sql | VT-202 alerts |
| 038_pipeline_steps_fts.sql | tsvector + GIN (VT-216) |
| 039_admin_audit_log.sql | Admin audit (VT-224) |
| 040_tenant_drive_channels.sql | Drive Push channels (VT-222) |

---

*This document is an index + briefing. Depth lives in the substrate it points at. Treat any divergence between this doc and the substrate as a doc-rot bug to fix on next refresh trigger.*
