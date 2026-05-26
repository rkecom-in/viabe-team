---
task: VT-178
author: claudecode
ts: 2026-05-26T18:59:00+05:30
estimated_tokens: 60000
estimated_minutes: 60
classification: critical-path-substrate
follows: VT-30
---

## TL;DR

STEP-0 surfaces SIGNIFICANT drift between brief's design-doc §2.1 spec and actual on-main schema for all 3 tables. Brief framed this as "small audit + maybe hardening"; actual delta is substantial. Three plan-ready Qs (all narrow but Q1 is load-bearing). Plan ships: (a) new migration `024_pipeline_observability_indexes.sql` adding missing composite indexes, (b) documentation drift in module docstrings, (c) canary verifying actual schema + RLS + new indexes. Deferring schema migration to a downstream row.

## Detailed drift surfaced at STEP-0

**Schema columns (actual on-main vs brief §2.1):**

`pipeline_runs` actual:
- `id, tenant_id, run_type, status, started_at, ended_at, trigger_payload (JSONB), terminal_state_metadata (JSONB), cost_paise`

Brief §2.1 says:
- `run_id, tenant_id, trigger_kind, trigger_source_ref, started_at, ended_at, status, final_outcome, total_cost_paise, step_count, error_summary`

Missing-from-actual: `final_outcome`, `step_count`, `error_summary`, `trigger_kind`, `trigger_source_ref`. Actual has JSONB stand-ins (`terminal_state_metadata`, `trigger_payload`).

`pipeline_steps` actual:
- `id, run_id, tenant_id, step_index, step_kind, input_envelope, output_envelope, rationale, started_at, ended_at, cost_paise, duration_ms, error_envelope`

Brief §2.1 says additionally:
- `step_name, parent_step_id, tool_calls, status, model_used, tokens_input, tokens_output`

Missing-from-actual: 7 columns.

`phone_token_resolutions` actual:
- `token, tenant_id, phone_number_encrypted, resolved_count, last_resolved_at, created_at`

Brief §2.1 says:
- `phone_token, tenant_id, customer_id, phone_e164, created_at, last_accessed_at`

Names + columns differ.

**RLS:**

All 3 tables: ENABLE+FORCE RLS ✓ with 4 policies (SELECT/INSERT/UPDATE/DELETE) using `app_current_tenant()` ✓. BUT `phone_token_resolutions` has SAME RLS as the other 2 — NOT stricter as brief requires.

**Indexes:**

- `pipeline_runs (tenant_id)` exists; brief wants `(tenant_id, started_at DESC)` — MISSING composite.
- `pipeline_steps (run_id)` + `(tenant_id)` separate; brief wants `(run_id, step_seq)` + `(tenant_id, started_at DESC)` — MISSING composites (and `step_seq` column is actually `step_index`).
- `phone_token_resolutions (tenant_id)` exists; brief wants `(tenant_id, phone_token)` — column is actually `token`; missing composite.

## Approach (conditional on Q1)

### Path (b) — recommended — RECOGNIZE actual schema; HARDEN indexes; DEFER stricter RLS

1. **NEW migration `024_pipeline_observability_indexes.sql`** — additive only:
   - `CREATE INDEX pipeline_runs_tenant_started_idx ON pipeline_runs (tenant_id, started_at DESC)`
   - `CREATE INDEX pipeline_steps_run_step_idx ON pipeline_steps (run_id, step_index)`  (uses `step_index` not `step_seq`)
   - `CREATE INDEX pipeline_steps_tenant_started_idx ON pipeline_steps (tenant_id, started_at DESC)`
   - `CREATE INDEX phone_token_resolutions_tenant_token_idx ON phone_token_resolutions (tenant_id, token)`
   - Single-column indexes from 005/006/007 kept (no DROP — composite + single coexist safely; query planner uses whichever is best).

2. **Documentation amendments** to module docstrings of `005_pipeline_runs.sql` + `006_pipeline_steps.sql` + `007_phone_token_resolutions.sql`:
   - Cite VT-178 + the design-doc §2.1 drift
   - Note the JSONB stand-ins (`trigger_payload`, `terminal_state_metadata`, `error_envelope`) cover the spec's separate-column intent
   - Document that `step_index` is the actual column name (vs spec's `step_seq`)
   - Document that `token` is the actual column name (vs spec's `phone_token`)
   - **No migration of column names** in this row — every existing consumer (VT-102/103/104/171/175/176/30) reads the actual names

3. **Documentation amendment** to `.viabe/sprint/VT-178.md` and/or active-context-summary about the design-doc-vs-actual reconciliation
   - Brief's §2.1 list serves as future-aspirational shape; consumers use actual columns
   - When VT-122's main row lands, design-doc §2.1 should be updated to match actual schema

4. **NO new RLS policies** — keep `app_current_tenant()` semantics. Stricter RLS for `phone_token_resolutions` (operator-role discrimination) is a NEW SUBSTRATE feature (requires defining `app_operator_role` distinct from `app_role`). Deferring to a follow-up VT row (Q2 confirms).

### Path (a) — NOT recommended — migrate schema to design-doc §2.1

Adds columns to all 3 tables + renames + back-fills. Blast radius into 7 existing canaries + production code paths. Should ship as its own dedicated VT row with full regression sweep; not in scope for VT-178.

## File changes

- **NEW** `migrations/024_pipeline_observability_indexes.sql` (additive composite indexes)
- **MODIFY** `migrations/005_pipeline_runs.sql` — append a leading comment block citing VT-178 + drift documentation. NO DDL changes; just docstring update at the top.
- **MODIFY** `migrations/006_pipeline_steps.sql` — same as 005.
- **MODIFY** `migrations/007_phone_token_resolutions.sql` — same.
- **NEW** `apps/team-orchestrator/canaries/vt178_pipeline_tables_rls.py` — 8 assertions (column audit verifies ACTUAL schema not design-doc; RLS audit confirms current policies; index audit confirms the 4 new composites land).
- **NO MODIFICATIONS** to application code paths (this row is observability substrate; the existing readers/writers use actual columns already).

## Test plan

- Apply migration 024 to dev DB locally before canary
- Run VT-178 canary 8/8 PASS
- 6-canary regression sweep: VT-102 + VT-103 + VT-104 + VT-171 + VT-175 + VT-176 byte-identical (all 6 use these tables; adding indexes is additive — should not perturb)
- `ruff check apps/team-orchestrator` — clean
- `pytest tests/` — broader sweep zero regression

## Risks

1. **Q1 — Schema drift between brief §2.1 and actual schema is significant** (above). Recommend Path (b) — document drift, ship indexes only.

2. **Q2 — Stricter RLS for phone_token_resolutions requires new substrate** (an `app_operator_role` distinct from `app_role`). Brief says "operator role required for resolution, NOT just tenant role". This is a NEW role + new policy + a new connection wrapper. Significant scope. **Recommend defer to a follow-up VT row** (VT-178.X or similar; can land before VT-186). Plan Q2 confirms.

3. **Q3 — Canary Group A column audit framing.** Brief expects design-doc §2.1 column list; actual schema differs substantially. Canary will assert ACTUAL columns (not design-doc). Test failure surfaces drift via explicit comparison if the schema ever changes. **Confirming this approach is acceptable.**

4. **Migration ordering.** Migration 023 (VT-175) already on main. New migration is 024. No conflicts.

5. **6-canary regression sweep budget** ~173s — within audit window, not canary wall-clock.

6. **CONCURRENTLY index creation:** since the tables may have data, use `CREATE INDEX CONCURRENTLY` to avoid table-lock. **However:** the migration runner wraps DDL in a transaction by default; `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. **Mitigation:** the indexes ship as plain `CREATE INDEX` (regular index creation; brief takes a short table lock; dev DB is empty enough that this is fine; prod is also Phase-1 with low row counts). If concurrency-safe creation is required, can be revisited in a follow-up.

## Plan-ready questions

### Q1 — Schema drift handling (load-bearing)

**Recommend Path (b):** keep actual schema as canonical; document drift in module docstrings; design-doc §2.1 updated post-merge to match actual. No column migrations in this row.

**Alternative:** Path (a) migrate to §2.1 — adds ~5-7 new columns per table, renames, possibly back-fills. Blast radius into 7+ existing consumers. Should ship as dedicated VT row.

### Q2 — Stricter RLS for phone_token_resolutions

Brief requires "operator role required for resolution, NOT just tenant role." Implementing means defining a new `app_operator_role` (distinct from `app_role`), a new `tenant_connection_operator()` wrapper, and a CASE-based RLS policy. Significant new substrate.

**Recommend defer** to a follow-up VT row (VT-178.X or similar; lands before VT-186 / VT-123 Ops UI). This row stays small + audit-focused per brief intent. Document the deferred-strictness in module docstring.

### Q3 — Canary Group A column-audit framing

Canary asserts ACTUAL on-main column names (verified at STEP-0). If/when the design-doc §2.1 schema lands via a future migration, that future row updates the canary. **Confirming this is acceptable.**

## Status

`.viabe/queue/VT-178/status` flipped `queued` → `planning` → `review`. Signalling plan-ready. Will proceed on APPROVED.
