# migrations/

Canonical migration directory for the **shared** Postgres database
(`viabe-team-prod` / `viabe-team-dev`).

## Ownership

All three Viabe Team apps — `team-web`, `team-orchestrator`, and
`team-ingestion-worker` — read and write the **same** Postgres database.
That schema is owned here, at the repo root, and nowhere else.

**Do not create per-app migration directories** (Pillar 8 — no patchwork).
There is exactly one migration history and one runner.

## Files

| File | Purpose |
| --- | --- |
| `000_init.sql` | Placeholder (empty). |
| `000a_extensions.sql` | `pgvector`. (Apache AGE deferred to VT-7.) |
| `000b_rls_helpers.sql` | `app_current_tenant()` — reads the tenant GUC for RLS. |
| `001_tenants.sql` | `tenants` + RLS. |
| `002_phase_transitions.sql` | `phase_transitions` + RLS. |
| `003_subscriptions.sql` | `subscriptions` + RLS. |
| `004_razorpay_webhook_events.sql` | `razorpay_webhook_events` (workspace-wide). |
| `005_pipeline_runs.sql` | `pipeline_runs` + RLS. |
| `006_pipeline_steps.sql` | `pipeline_steps` + RLS. |
| `007_phone_token_resolutions.sql` | `phone_token_resolutions` + RLS. |
| `008_privacy_audit_log.sql` | `privacy_audit_log` + RLS. |
| `009_env_config.sql` | `env_config` (workspace-wide). |

## Conventions

- Files are ordered and apply-once: `000_init.sql`, `001_*.sql`, … .
- Never edit a migration that has already been applied — add a new one.
- **Pillar 3**: every tenant-scoped table enables (and `FORCE`s) Row-Level
  Security and defines its policies *in the same migration that creates it* —
  never as a follow-up. Workspace-wide tables (`004`, `009`) get a deny-all
  policy; only the RLS-bypassing service role reaches them.

## Running migrations

The single runner is
[`apps/team-orchestrator/scripts/apply_migrations.py`](../apps/team-orchestrator/scripts/apply_migrations.py).
It reads a direct Postgres DSN from `DATABASE_URL` (or `TEAM_SUPABASE_DB_URL`),
tracks applied files in `schema_migrations`, and is idempotent.

```bash
cd apps/team-orchestrator
DATABASE_URL=postgres://… uv run python scripts/apply_migrations.py
```

## RLS tenant context

Tenant-scoped policies key off the `app.current_tenant` session GUC. The
application's typed wrappers (VT-8) set it per request:

```sql
SELECT set_config('app.current_tenant', '<tenant-uuid>', false);
```

An un-scoped connection matches no tenant rows. The Supabase secret key /
Postgres superuser bypasses RLS entirely — the intended service-role path.

## Generated types

Generated type definitions for this schema land in
[`packages/team-shared/db/`](../packages/team-shared/db/) (VT-Foundation
follow-up / VT-8). Do not hand-write them.
