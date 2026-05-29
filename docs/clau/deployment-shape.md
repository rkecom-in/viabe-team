# Deployment shape — architecture of record (VT-120)

Captures the Viabe Team deployment topology as of 2026-05-29. This document is **authoritative** and supersedes VT-17 through VT-21 (which presumed a monorepo). Future Claude sessions reading those old rows should defer to this doc. Pure design doc — NO infra changes in this PR. Mismatches surfaced during writing are filed as new VT rows (see Section 8).

---

## 1. Why this doc

Three architectural decisions are now load-bearing and need a single canonical reference:

- **2026-05-04 Fazal correction** — moved from a planned monorepo to three sibling repos. VT-17 through VT-21 still describe the monorepo; this doc supersedes that framing.
- **2026-05-28 Railway/Vercel dev env shipped** — VT-218 through VT-221 wired the orchestrator (Railway) + team-web (Vercel) + CI deploy substrate (`deploy-dev.yml`) for `viabe-team`'s dev environment. Smoke-tested + identity-validated.
- **2026-05-29 CL-421 zero-paste connector discipline** — drove VT-222 Sheet redesign + applies forward to every future connector (ADR-0004).

Without this doc, every fresh session has to reconstruct the deployment story from scattered runbooks + sprint rows. With it, one read covers the substrate.

## 2. Top-level shape

Three independent GitHub repositories under `rkecom-in/`:

| Repo | Surface | Hosting |
|---|---|---|
| `viabe-team` | Multi-agent system for SMB owners | Railway (orchestrator) + Vercel (web + ops console) |
| `viabe-reports` | Analytic reports product | Vercel |
| `viabe-marketing` | Marketing site | Vercel |

All three serve `viabe.ai` under one domain via path-based rewrites (ADR-0006):

```
viabe.ai/         → viabe-marketing
viabe.ai/team/*   → viabe-team (team-web)
viabe.ai/report/* → viabe-reports
```

One SSL cert. One CDN. One canonical domain. Cookies scoped per path (operator-JWT only at `/team/ops/*`).

Two Supabase projects per `viabe-team`:

- **viabe-team-dev** — `ap-south-1` (Mumbai) — **pending VT-169 verification** (the pooler hostname surfaces `ap-northeast-2` per VT-102 canary; canary will confirm whether this is Interpretation 3 / pooler topology, or Interpretation 1 / real misconfig)
- **viabe-team-prod** — `ap-south-2` (Hyderabad) — **to-provision** (does not exist yet; provisioning is a Fazal-side gate before launch milestone Reports-Jun15)

## 3. viabe-team repo internals

```
viabe-team/
├── apps/
│   ├── team-web/             Next.js 16 + React 19; deployed to Vercel
│   ├── team-orchestrator/    Python 3.13 + DBOS + LangGraph; deployed to Railway
│   └── team-ingestion-worker/  SystemExit stub (planned Apify + Sarvam worker)
├── packages/
│   └── team-shared/          cross-app types (TypeScript)
├── migrations/               numbered .sql files (037 latest at time of writing; 040 reserved for VT-222)
├── docs/
│   ├── adr/                  9 ADRs (VT-117)
│   ├── runbooks/             ops runbooks (VT-118)
│   └── clau/                 architecture-of-record + decisions-ledger + runbooks (this doc)
└── .viabe/                   Cowork/CC operating layer (sprint rows + queue + secrets + protocol)
```

### apps/team-web

- Next.js 16, React 19, Tailwind, App Router under `app/(app)/...`
- Routes: `/team/onboard` (owner onboarding wizard), `/team/dashboard/*`, `/team/ops/*` (Fazal-gated)
- Server-side fetches via `@/lib/ops/data-access.ts` directly against Supabase
- Operator JWT via `@/lib/auth/operator-jwt.ts` (HS256) + `requireFazal()` gate
- Realtime substrate via Supabase Realtime + REPLICA IDENTITY FULL + operator-claim RLS

### apps/team-orchestrator

- Python 3.13, uv-managed (lockfile in repo); FastAPI + DBOS + LangGraph
- DBOS workflows: ingestion (5-min cron, VT-210), purge (VT-200), drive-push delta (VT-222), alerts sweep + daily digest (VT-202), Drive channel renewal (6-hour, VT-222), polling fallback (10-min, VT-222)
- LangGraph supervisor + multiple agents (SR-Agent stub, Integration Agent, Orchestrator Agent)
- Schedulers registered imperatively in `main.py` lifespan BEFORE `launch_dbos()` (CL-220, VT-200/215 lesson)
- HTTP API under `/api/orchestrator/...` (Twilio webhook, OAuth callback, Drive Push webhook, admin endpoints, etc.)
- Multi-stage Dockerfile (`python:3.13-slim` + uv + tini); WORKDIR `/repo/apps/team-orchestrator` mirrors monorepo so uv.lock's `editable = "../../packages/team-shared"` resolves (VT-218 Dockerfile fix)

### apps/team-ingestion-worker

- Currently a SystemExit stub
- Planned: Apify actor management + Sarvam ASR + WhatsApp media ingestion; deferred past Phase 1

## 4. Deploy pipeline

Push to `main` triggers parallel deploys via `.github/workflows/deploy-dev.yml`:

```
main push
├── Railway deploy (team-orchestrator)
│   └── docker build + push + service update (RAILWAY_TOKEN + RAILWAY_SERVICE_ID secrets)
├── Vercel deploy (team-web)
│   └── only if apps/team-web/ changed (per vercel.json ignoreCommand) (VT-223)
└── post-deploy smoke (scripts/dev-env-smoke.sh)
    └── 4 assertions: orchestrator /health 200, /team/onboard 302→/login,
        /team/ops/stream 302→/login, internal endpoint 401 without secret
```

`vercel.json` settings (VT-223):
- `framework: nextjs`
- `regions: ["bom1"]` — Mumbai edge
- `ignoreCommand: "git diff --quiet HEAD^ HEAD ./"` — skip build when diff doesn't touch `apps/team-web/`
- `github.silent: false` — Vercel PR status checks render (VT-223 flipped from true; per VT-221 surfaced)

PR-side preview deploys are enabled per VT-223 (Fazal dashboard toggle).

## 5. Connector discipline (CL-421)

Every Integration Agent connector MUST be zero-manual-paste after OAuth. ADR-0004 captures this in full. Implications for any new connector:

- OAuth grant → auto-configure via vendor API; no copy-paste; no developer-shaped setup
- Google Sheet (VT-222): OAuth → Drive Push channel auto-registered; 6-hour renewal scheduler; 10-min polling fallback when push channel expired/missing
- Shopify (VT-208 / VT-213): OAuth → Admin API auto-subscribes webhook topics
- Future connectors (Stripe, Razorpay, etc.): same constraint applies at brief time

`setup_push` + `apps_script_template` (VT-207 PR-1 substrate) deprecated; kept for backward compat with pre-VT-222 tenants. Existing Apps-Script-onboarded tenants migrate via opt-in re-OAuth grants per `docs/clau/sheet-integration-runbook.md`.

## 6. Auth model

Two parallel auth substrates (ADR-0008):

- **operator-JWT** — Fazal session at `/team/ops/*`; HS256 with `OPERATOR_JWT_SECRET`; 1-hour TTL; HttpOnly Secure cookie path-scoped; magic-link sign-in via Supabase Auth at `/team/ops/login` (VT-203)
- **admin-token** — `TEAM_ADMIN_API_TOKEN` (32-byte hex) on `X-Team-Admin-Token` header for `/api/orchestrator/admin/*` (VT-224); in-process 10 req/sec rate limit per token; every call writes one `admin_audit_log` row with 8-char sha256 fingerprint

Per-tenant data isolation via Supabase RLS (`app_current_tenant()` policy). Operator-claim JWT bypasses tenant RLS for Ops Console reads (`pipeline_steps`, `pipeline_runs`, `tenant_alerts`). Admin endpoints use service role (intentional cross-tenant operations).

## 7. Secrets

Two layers:

- **Railway env** — orchestrator runtime config (DATABASE_URL, ANTHROPIC_API_KEY, GOOGLE_OAUTH_*, TWILIO_*, TEAM_ADMIN_API_TOKEN, TEAM_PHONE_ENCRYPTION_KEY, OPERATOR_JWT_SECRET, FAZAL_TENANT_ID, FAZAL_OWNER_UUID, RESEND_API_KEY, TELEGRAM_BOT_TOKEN, etc.). Set via dashboard. Rotation procedures per-secret in `docs/clau/admin-endpoints-runbook.md` (admin token) and `docs/clau/dev-env-runbook.md` (others).
- **`.viabe/secrets/*.env`** — local dev only; subshell-source pattern (`( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a; ./.venv/bin/python ... )`). NEVER `export` into parent shell. NEVER commit to git.

CL-390 cluster: phone encryption key (`TEAM_PHONE_ENCRYPTION_KEY`) stays in orchestrator process only (defense-in-depth). OAuth refresh tokens encrypted at rest via Fernet wrap (shared helper) using the same key.

## 8. Known follow-up rows

No mismatches surfaced at doc-authoring time. Section 8 is currently empty. Future mismatches discovered while reading this doc against repo/Railway/Vercel state should be filed as new VT rows + referenced here as `VT-NN: <one-line summary>`.

---

## Cross-refs

- ADR-0001 (DBOS substrate) through ADR-0009 (memory tiering) — `docs/adr/`
- CL-41 (three-repo architecture), CL-79 (Postgres single-substrate), CL-132 (path routing), CL-220 (operator-JWT), CL-390 (privacy cluster), CL-416 (lifetime retention), CL-417 (canonical schema), CL-418 (Rule #17), CL-421 (zero-paste connectors) — `docs/clau/decisions-ledger.md`
- VT-17 through VT-21 — **SUPERSEDED by this doc** (described monorepo; framing wrong)
- VT-218 through VT-223 — dev environment substrate
- VT-222 — Drive Push connector substrate
- VT-224 — admin endpoints suite
- VT-169 — region residency verification (pending)
