# migrations/

Canonical migration directory for the **shared** Postgres database
(`viabe-team-prod`).

## Ownership

All three Viabe Team apps — `team-web`, `team-orchestrator`, and
`team-ingestion-worker` — read and write the **same** Postgres database.
That schema is owned here, at the repo root, and nowhere else.

**Do not create per-app migration directories.** A migration under
`apps/*/` would fragment ownership of a shared resource and let two apps
disagree about the schema. There is exactly one migration history.

## Conventions

- Files are ordered and apply-once: `000_init.sql`, `001_*.sql`, … .
- Never edit a migration that has already been applied — add a new one.
- `000_init.sql` is currently an empty placeholder. The first real schema
  lands in **VT-Foundation**.

## Generated types

When the first real migration lands, generated type definitions for the
schema go in [`packages/team-shared/db/`](../packages/team-shared/db/) so
all apps consume one typed view of the database.
