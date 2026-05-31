---
vt_id: VT-2
title: VT-Foundation — repo, Supabase, RLS, secrets, dev/prod, CI
status: Done
priority: Critical
sprint: Sprint 1 - Foundation
type: Infrastructure
area: [Infrastructure, Database, DevOps]
assignee: Clau
parent: ""
sub_items: [VT-17, VT-18, VT-19, VT-20, VT-21, VT-22, VT-23, VT-120]
exec_order: 1
branch: "feat/vt-foundation"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-819f-9e17-e9f61f48eb3c
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-2 — VT-Foundation — repo, Supabase, RLS, secrets, dev/prod, CI

## Why this parent exists
Reports product accumulated infrastructure tech debt through ad-hoc setup. Auth keys leaked into `.env.example`, Supabase RLS was retrofitted, secrets were not rotated when scope changed. Team product cannot afford that. By the time the first MCP tool lands, the foundation must already enforce tenant isolation through RLS, secret discipline, environment separation between dev and prod, and CI gates that catch architecture violations before merge. Foundation is critical-path because every later parent — orchestrator, agent, tools, ingestion, knowledge, owner surface, billing — sits on top of it.
This parent is also where the Reports/Team coexistence boundary gets enforced. The two products share a monorepo, a Vercel team, a Railway organization, and a Supabase organization, but they share zero application code. Cross-product imports are forbidden by CI lint, not just convention.

## What this parent owns
1. Monorepo layout: `apps/team-web/`, `apps/team/`, `packages/team-shared/` directories. Reports apps remain untouched. No cross-product imports either direction; CI greps prove it.
2. Two Supabase projects: `viabe-team-dev` (region: ap-south-1, Mumbai) and `viabe-team-prod` (region: ap-south-2, Hyderabad), both with point-in-time recovery and daily snapshots configured.
3. The 8-table base schema with `tenant_id NOT NULL` on every multi-tenant row, plus 32 RLS policies covering SELECT/INSERT/UPDATE/DELETE on every multi-tenant table, plus 7 attack tests proving cross-tenant access is impossible by construction.
4. Vercel and Railway dev and prod environments wired to GitHub Actions, with branch protection rules: `main` requires PR + CI green + Fazal review; `dev` requires CI green; no direct pushes to either.
5. `INTERNAL_API_SECRET` discipline: 64-character random secret, different value per environment, shared across web/orchestrator/worker, never logged, compared constant-time only.
6. Base CI: pytest (Python), vitest (TS), ESLint, Black, Notion task ID checker (PR title must reference `(VT-N)`), gitleaks (secret scanning). Every PR must pass to merge.

## Architectural rules binding every subtask
- Pillar 3 (tenant isolation is structural, not procedural): every multi-tenant table has `tenant_id NOT NULL`. RLS policies are added in the same migration that creates the table, never as a follow-up. Three independent enforcement layers must exist: Postgres RLS, application typed wrappers (added in VT-8), and agent context isolation (added in VT-4/VT-8).
- Pillar 8 (no patchwork): no manual hotfixes to production data. All schema changes route through versioned migrations in `packages/team-shared/migrations/`.
- Supabase key discipline: only `SUPABASE_PUBLISHABLE_KEY` and `SUPABASE_SECRET_KEY` are permitted. The legacy `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` are forbidden in code, env files, and docs. CI greps for them and fails the build if found.
- Secret rotation: every shared secret has a documented rotation owner and frequency. Any secret change must be reflected in the runbook in the same PR.
- Dev/prod isolation: `dev` branch deploys to dev environments only. `main` deploys to prod only when Fazal explicitly merges. No automation can promote dev → main.

## Subtasks under this parent
1. **VT-2.1** — Repo and monorepo layout: `apps/team-web/`, `apps/team/`, `packages/team-shared/`.
2. **VT-2.2** — Supabase dev (ap-south-1) and prod (ap-south-2) projects with backups.
3. **VT-2.3** — Base schema: 8 core tables plus `tenant_id` column on every multi-tenant table.
4. **VT-2.4** — 32 RLS policies + 7 attack tests proving no cross-tenant read/write/update/delete is possible.
5. **VT-2.5** — Vercel and Railway dev and prod environments with GitHub Actions deploys.
6. **VT-2.6** — `INTERNAL_API_SECRET` wiring across web, orchestrator, and worker services with constant-time compare.
7. **VT-2.7** — Base CI: pytest + vitest + lint + Notion task ID check + gitleaks; PRs must pass to merge.

## Definition of done
- All 7 subtasks Done.
- Smoke test passes end-to-end on dev: a write via the typed wrapper succeeds for tenant A and is invisible to tenant B (proven by RLS attack test).
- CI pipelines green on a sample PR. Gitleaks catches a known fake leak in a test commit (proven by intentional negative test).
- Runbooks for secret rotation and Supabase backup restore exist at `docs/runbooks/` and have been read by Fazal.
- A team member with only `SUPABASE_PUBLISHABLE_KEY` cannot write to the database. A team member with only `SUPABASE_SECRET_KEY` can write only via typed wrappers (RLS still enforced).

## Out of scope
- LangGraph orchestrator code (VT-3).
- The Anthropic Agent SDK skeleton (VT-4).
- MCP tool framework and individual tools (VT-5).
- Apache AGE setup and pgvector knowledge stores (VT-7).
- Privacy-specific machinery (typed wrapper enforcement, k-anon admission, opt-out flow) — that lives in VT-8.
- Owner-facing surfaces (VT-9), billing (VT-10), landing site (VT-11).

## Branch convention
- Parent branch: `feat/vt-foundation`.
- Subtask branches: `feat/vt-foundation-<short>` (e.g. `feat/vt-foundation-supabase-rls`).
- PR title format: `<type>(foundation): <description> (VT-2.N)`.
- Reviewers: CoderC for implementation; CoderX for second-pass review on RLS, secrets, and CI configuration.
- Merge target: `dev`. Promote to `main` only after all VT-2 subtasks Done and Fazal merges.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-819f-9e17-e9f61f48eb3c)

## Closed 2026-05-31 (delivered-umbrella, closeout batch)
All granular children delivered + merged; epic marked Done as a delivered umbrella. See decisions-ledger + latest-snapshot.
