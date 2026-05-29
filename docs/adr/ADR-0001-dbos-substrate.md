# ADR-0001: DBOS as durable-workflow substrate (not Temporal)

**Status:** Accepted

## Context

Viabe Team's orchestrator needs durable workflows: schedule-driven ingestion (every 5 min), webhook delivery handling, multi-step agent execution that survives process restarts. Two mainstream choices in this space:

- **DBOS** — Postgres-as-the-substrate; workflow state, schedules, recovery all live in the same DB the application already uses. No separate cluster, no separate operator burden. Postgres-on-Supabase is already mandatory (CL-79).
- **Temporal** — dedicated workflow engine; separate cluster + worker fleet; battle-tested at scale; richer DAG modeling.

## Considered Options

- **A.** Temporal — separate operator surface; idiomatic for workflow-heavy systems; needs a hosted cluster or Temporal Cloud
- **B.** DBOS — substrate-coupled; one fewer thing to operate; same Postgres connection pool the rest of the app uses (chosen)
- **C.** Hand-rolled cron + state table — initial simplicity but every new workflow re-invents recovery semantics; rejected

## Decision

**B (DBOS).** Sprint 1 locked Postgres-on-Supabase as the sole stateful substrate (CL-79). DBOS reuses that substrate for workflow state, scheduler, recovery, and idempotency keys. Zero net infra at the cost of accepting DBOS's narrower DAG model (which Viabe Team's workflows happily fit).

## Consequences

- (+) Single Postgres to operate, back up, and restore. No Temporal cluster.
- (+) Workflow state co-located with business data — atomic transactions span both.
- (+) Schedulers registered imperatively (`register_purge_scheduler`, `register_ingestion_scheduler`, `register_drive_push_scheduler`) — known shape; lessons captured in VT-200 / VT-215.
- (−) DBOS's recovery semantics are tied to Postgres availability — if Supabase is down, workflows are paused (acceptable for Phase 1).
- (−) DBOS's DAG model is narrower than Temporal's (no native saga compensation); we work around with explicit error envelopes.
- (−) Smaller community than Temporal — fewer Stack Overflow answers; we read DBOS source occasionally.

## References

- CL-36 (DBOS workflow substrate decision)
- CL-79 (Postgres-on-Supabase as sole substrate)
- CL-220 (lifespan ordering — DBOS launched after schedulers registered)
- VT-200, VT-210, VT-215 (DBOS workflow + scheduler decoration pattern)
- DBOS docs: https://docs.dbos.dev/
