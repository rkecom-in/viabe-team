# @viabe/team-shared

Shared types used across the Viabe Team apps, in two language surfaces:

- `src/` — TypeScript, consumed by `apps/team-web`.
- `python/team_shared/` — Python, consumed by the orchestrator and ingestion
  worker.

## Codegen workflow

The two surfaces must stay in sync. Phase 1 keeps them hand-written; the
codegen pipeline (single source of truth → generated TS + Python) lands in a
later ticket. Until then:

> When you change a shared type, edit **both** `src/index.ts` and
> `python/team_shared/__init__.py` in the same PR.

No business logic lives here — types only.
