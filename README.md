# Viabe Team

Monorepo for **Viabe Team** — Phase 1.

This repo is a product-scoped monorepo. It owns its own config, types, and
deploy surface. It must not depend on or read another Viabe product's
environment (see [No cross-product env vars](#no-cross-product-env-vars)).

## Architecture overview

Viabe Team is three deployable apps plus one shared package:

| App | Stack | Role |
| --- | --- | --- |
| `apps/team-web` | Next.js 15, React 19, TypeScript (strict) | Marketing site, owner dashboard, Fazal-only Ops UI, webhook receivers |
| `apps/team-orchestrator` | Python 3.12, DBOS, LangGraph, `langgraph_supervisor`, Anthropic SDK | Durable multi-agent workflow engine |
| `apps/team-ingestion-worker` | Python 3.12, Apify SDK, Sarvam AI, Anthropic SDK | Heavy data ingestion |
| `packages/team-shared` | TypeScript + Python (via codegen) | Shared types across apps |

Reference docs (Notion):

- **Concept doc** — https://www.notion.so/35e387c2cc5a817fb9e6d16a73167559
- **Architecture diagrams** — https://www.notion.so/35f387c2cc5a81c2a1cfed73b080f931
- **Execution plan** — https://www.notion.so/360387c2cc5a81328354cf6ea6c8ee7c
- **121-subtask audit** — https://www.notion.so/361387c2cc5a81c8b19dce85d44f873c
- **ViabeTeam_Sprint DB** — https://www.notion.so/datasources/20c8c0cc-7ba5-41cb-999e-77246cdefc51
- **Viabe_Launch_Tracker DB** — https://www.notion.so/datasources/413be4ab-870d-4895-bf35-dfd579142001
- **Clau_Session_Log DB** — https://www.notion.so/datasources/76e76a8e-ac24-4976-a48c-7311cf3ed6ca

### Repo layout

```
apps/
  team-web/                 Next.js 15
    app/(marketing)/team/    Landing page + founding-counter widget
    app/(app)/team/dashboard/  Owner portal (read-only, 4 views)
    app/(app)/team/ops/        Fazal-only Ops UI (3 MVP views)
    app/api/team/.../webhook   Twilio + Razorpay webhook receivers
  team-orchestrator/        Python — DBOS workflows
  team-ingestion-worker/    Python — Apify ingestion
packages/
  team-shared/              Shared TS + Python types
    db/                     Generated DB types (populated in VT-Foundation)
migrations/                 Canonical migrations for the shared Postgres DB
scripts/                    Repo tooling (lint rules)
```

## Local dev setup

Prerequisites: **Node 22** (`.nvmrc`), **pnpm 10**, **Python 3.12**, **uv**.

```bash
# JS/TS workspaces
corepack enable pnpm        # or: npm install -g pnpm@10
pnpm install

# Python apps (per app)
cd apps/team-orchestrator && uv sync && cd -
cd apps/team-ingestion-worker && uv sync && cd -

# Env — each app has its own .env.example; copy to .env.local
cp apps/team-web/.env.example             apps/team-web/.env.local
cp apps/team-orchestrator/.env.example    apps/team-orchestrator/.env.local
cp apps/team-ingestion-worker/.env.example apps/team-ingestion-worker/.env.local
```

Common commands (run from repo root):

```bash
pnpm lint        # cross-product env rule + ESLint
pnpm typecheck   # tsc across workspaces
pnpm test        # vitest (repo tooling unit tests)

pnpm --filter @viabe/team-web dev   # Next.js dev server
```

## Environment variables

Each app owns its own `.env.example` — there is **no root `.env.example`**.
This matches the deployment topology (Vercel reads `apps/team-web`'s env;
Railway reads each backend service's) and stops a server secret and a
client-bundled var from ever sharing one file.

| App | File | Holds |
| --- | --- | --- |
| `team-web` | [`apps/team-web/.env.example`](./apps/team-web/.env.example) | Frontend-bound vars only — all `NEXT_PUBLIC_*` |
| `team-orchestrator` | [`apps/team-orchestrator/.env.example`](./apps/team-orchestrator/.env.example) | Server secrets — Supabase, Twilio, Razorpay, Anthropic, Resend, pricing, `INTERNAL_API_SECRET` |
| `team-ingestion-worker` | [`apps/team-ingestion-worker/.env.example`](./apps/team-ingestion-worker/.env.example) | Server secrets — Supabase, Apify, Sarvam, Anthropic, `INTERNAL_API_SECRET` |

Local dev: copy each app's `.env.example` to `.env.local` in that app
directory (see [Local dev setup](#local-dev-setup)).

### Environment variable rules

CI enforces these via the lint rule
([`scripts/lint-cross-product-env.mjs`](./scripts/lint-cross-product-env.mjs)),
which fails the build on a violation:

- **No cross-product vars** — any name prefixed `REPORTS_` (Viabe Reports).
- **No deprecated Supabase keys** — `SUPABASE_ANON_KEY` and
  `SUPABASE_SERVICE_ROLE_KEY`. Only `TEAM_SUPABASE_PUBLISHABLE_KEY` and
  `TEAM_SUPABASE_SECRET_KEY` are permitted.
- **Per-app env separation** — server secrets (`*_SECRET_KEY`, `*_AUTH_TOKEN`,
  `*_API_KEY`) must not appear in `apps/team-web/.env.example`; `NEXT_PUBLIC_*`
  vars must not appear in a backend app's `.env.example`.

## DBOS workflow conventions

For `apps/team-orchestrator`:

- **Workflows** (`@DBOS.workflow()`) orchestrate; **steps** (`@DBOS.step()`)
  do the side-effecting work. Never put a side effect directly in a workflow
  body — it must be a step so it is checkpointed and not re-run on replay.
- Workflows must be **deterministic** and **idempotent**: same inputs →
  same path. No `random`, no clock reads, no direct I/O outside steps.
- Pass an explicit **idempotency key** for workflows triggered by webhooks
  or external events.
- Schema changes go through ordered SQL files in the repo-root
  [`/migrations/`](./migrations/) directory (`000_init.sql`, `001_*.sql`, …).
  This is the **single** migration directory — the `viabe-team-prod`
  Postgres database is shared by all three apps. Never edit an applied
  migration; never create a per-app migrations directory.
- Multi-agent graphs use `langgraph_supervisor`; the supervisor is the only
  agent that routes — sub-agents do not call each other directly.

## Branch & PR conventions

- `main` is protected: **no direct pushes**, PR required, and the
  `lint` / `typecheck` / `test` checks must pass before merge.
- Branch names: `feat/...`, `fix/...`, `chore/...`, `docs/...`.
- PR titles follow **Conventional Commits** and end with the ticket id, e.g.
  `feat(repo): initial scaffold (VT-17)`.
- Every PR is reviewed. **Fazal** owns sign-off on structure and tooling
  changes (see [`.github/CODEOWNERS`](./.github/CODEOWNERS)).
- Keep PRs scoped to their ticket — do not land business logic ahead of the
  ticket that owns it.
