# ADR-0003: Supabase Postgres as the sole stateful substrate

**Status:** Accepted

## Context

Viabe Team needs: tenant config (`tenants`, `tenant_oauth_tokens`), pipeline observability (`pipeline_runs`, `pipeline_steps`), workflow state (DBOS workflow tables), checkpoint state (LangGraph), L0 memory fragments, KG vectors (pgvector), Realtime substrate for Ops Console (replication slot + RLS-isolated subscriptions), per-tenant operator-claim RLS, DPDP-compliant data residency (ap-south-1/2).

Two paths:

- **Single Postgres** (Supabase) — one connection pool, one set of credentials, one operational surface, RLS across every table by `tenant_id = app_current_tenant()`
- **Polyglot** — Postgres + Redis (cache + rate limit) + S3 (large blobs) + dedicated vector DB (Pinecone / Weaviate)

## Considered Options

- **A.** Polyglot — best-of-breed per concern; high operator burden; consistency across stores is the dev's job; rejected for Phase 1
- **B.** Supabase Postgres single-substrate — accept Postgres limits (pgvector for vectors; PostgresSaver for LangGraph; PgBouncer for connection pooling); chosen
- **C.** Self-hosted Postgres on Railway/Fly — control + cost but no Realtime substrate, no auth-as-a-service, no managed PITR; rejected for Phase 1

## Decision

**B.** Supabase Postgres covers every stateful concern through Phase 1. pgvector handles L1 KG vectors (Apache AGE rejected per memory note). Realtime substrate (migration 030) backs the Ops Console live stream. RLS via `app_current_tenant()` is enforced at every table. Dual-project posture: `viabe-team-dev` (ap-south-1, pending VT-169 verification) + `viabe-team-prod` (ap-south-2, to-provision).

## Consequences

- (+) One connection pool, one credential, one PITR backup, one residency story
- (+) RLS is the single isolation contract — no leaking across `tenant_id` ever
- (+) Atomic transactions span business data + workflow state (DBOS reuses the same DB)
- (+) Realtime + Auth + Storage all on Supabase reduce additional vendors
- (−) Single point of failure — if Supabase is down, the orchestrator is down
- (−) Pgvector at L1 KG scale (>1M vectors) needs careful index sizing
- (−) Pooler-vs-DB region disambiguation needed (VT-169 surfaced this)
- (−) Supabase pricing scales with database size; lifetime retention (CL-416) needs cost modelling at Year 2+

## References

- CL-79 (Postgres-on-Supabase as sole substrate)
- CL-390 (per-table RLS via app_current_tenant())
- CL-416 (lifetime-of-relationship retention; DSR-purge only)
- VT-169 (region residency verification)
- VT-122 (pipeline observability schema)
- Memory: L1 KG drops Apache AGE; pgvector + relational only
