---
task: VT-175
author: claudecode
ts: 2026-05-26T13:35:00+05:30
estimated_tokens: 95000
estimated_minutes: 100
classification: critical-path-schema
unblocks: VT-176
---

## TL;DR

Migration `023_attributions_and_cadence_columns.sql` + `orchestrator/billing/{day39_evaluator,attribution_close}.py` + 2 test modules + 8-assertion zero-LLM canary + 5-canary regression sweep. One narrow architectural correction surfaces: the brief's example RLS policy references a GUC name (`app.tenant_id`) that doesn't match the repo's existing convention (`app.current_tenant` via `app_current_tenant()` helper from `migrations/000b_rls_helpers.sql`). Plan uses the existing helper to match every other RLS-bound table on main.

## Approach

### 1. Migration `023_attributions_and_cadence_columns.sql`

Closely follows the brief's schema spec, with two corrections + two strengthenings to match existing repo conventions (matches `016_campaigns.sql` template):

- **GUC correction:** RLS policy uses `app_current_tenant()` (the existing helper) instead of the brief's `current_setting('app.tenant_id')::uuid`. Same architecture; existing helper is the established pattern across 20+ tables. Surfaced as Q1.
- **FORCE RLS:** add `ALTER TABLE attributions FORCE ROW LEVEL SECURITY` per the campaigns / pipeline_log / every-other-tenant-table pattern. ENABLE alone leaves superuser bypassing; FORCE makes RLS apply even to table-owner (Pillar 3).
- **4 RLS policies (SELECT/INSERT/UPDATE/DELETE)** not just SELECT — matches campaigns template + tenant_connection's `SET ROLE app_role` flow.
- Indexes per brief; CHECK constraint per brief.
- `campaigns.attribution_close_at` / `attribution_closed_at` / `total_arrr_paise` — nullable additions per brief.
- `tenants.paid_conversion_at` — nullable per brief.

### 2. `orchestrator/billing/day39_evaluator.py`

Deterministic SQL — service-role connection via `get_pool().connection()` (not `tenant_connection`; day-39 reads cross-tenant for the eval window + writes the verdict event). NO LLM imports. Module body falls under the existing `gate-no-llm-in-deterministic-triggers` CI gate's scan once VT-176 wires it through `scheduled_triggers.day39_evaluation_scheduled`.

Plus pure Python comparison `arrr_paise >= 2 * cumulative_fees_paise`. Returns frozen dataclass `Day39Verdict(tenant_id, verdict, arrr_paise, cumulative_fees_paise, decided_at)` where `verdict ∈ {"continue", "refund_triggered", "not_eligible"}`. `not_eligible` covers tenants whose `paid_conversion_at + 39 days > now()`.

Idempotency: emit a `day39_continue` or `day39_refund_triggered` event; on re-run, query whether the event already landed for the tenant within the eval window; if yes, return the previously-decided verdict (frozen dataclass replay) without re-emitting. Pure SQL idempotency check; no surrogate idempotency table.

### 3. `orchestrator/billing/attribution_close.py`

Deterministic SQL — `service_role` connection; SUM(attributed_paise), UPDATE campaigns row, emit `attribution_closed` pipeline_log event. Returns `AttributionCloseResult(campaign_id, total_arrr_paise, already_closed: bool, closed_at)`. Idempotency: if `attribution_closed_at IS NOT NULL`, short-circuit and return `already_closed=True` without re-updating or re-emitting.

### 4. Tests

`tests/orchestrator/billing/` (new dir) with:
- `test_day39_evaluator.py` — pure tests for the comparison logic + integration-gated for both branches against real DB
- `test_attribution_close.py` — pure tests for the aggregation math + integration-gated for SQL path

### 5. Canary `canaries/vt175_attributions_and_day39.py`

8 assertions per brief. **Loader source list intentionally omits `anthropic.env`** — defense-in-depth proof that zero LLM can reach this path. Assertions verify Anthropic env var is ABSENT at preflight. Wall-clock ≤ 60s.

### 6. Event schemas

Add 3 new event types to `observability/event_schemas.py`:
- `attribution_closed` — released from VT-28's reserved list now that VT-175 supplies the body
- `day39_continue` — released
- `day39_refund_triggered` — released

VT-28 itself doesn't emit these — VT-176 wires them in. But schema registration belongs in VT-175 since `attribution_close.py` + `day39_evaluator.py` emit them.

## File changes

- **NEW** `migrations/023_attributions_and_cadence_columns.sql`
- **NEW** `apps/team-orchestrator/src/orchestrator/billing/__init__.py`
- **NEW** `apps/team-orchestrator/src/orchestrator/billing/day39_evaluator.py`
- **NEW** `apps/team-orchestrator/src/orchestrator/billing/attribution_close.py`
- **NEW** `apps/team-orchestrator/src/orchestrator/billing/types.py` — frozen dataclasses (`Day39Verdict`, `AttributionCloseResult`)
- **MODIFY** `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py` — register 3 released event types
- **NEW** `apps/team-orchestrator/tests/orchestrator/billing/__init__.py`
- **NEW** `apps/team-orchestrator/tests/orchestrator/billing/test_day39_evaluator.py`
- **NEW** `apps/team-orchestrator/tests/orchestrator/billing/test_attribution_close.py`
- **NEW** `apps/team-orchestrator/canaries/vt175_attributions_and_day39.py`

## Test plan

- `pytest tests/orchestrator/billing/ -q` — pure tests pass; integration gated on `RUN_INTEGRATION_TESTS=1`
- `pytest tests/` orchestrator-wide — zero regression
- `ruff check apps/team-orchestrator` — clean
- Apply migration to dev DB locally before canary
- Run canary 8/8 PASS against real Supabase dev pooler
- Cond-2-style regression sweep — VT-102 + VT-103 + VT-104 + VT-171 + VT-28 canaries all PASS byte-identical post-migration

## Risks

1. **GUC name mismatch in brief vs repo convention (the load-bearing one).** Brief's example uses `current_setting('app.tenant_id')::uuid`; repo's 20+ existing RLS-bound tables use `app_current_tenant()` (reads `app.current_tenant`). `tenant_connection()` wrapper sets `app.current_tenant` not `app.tenant_id`. **Q1 below — confirming I'll use `app_current_tenant()` to match every other table; semantically identical.**

2. **VT-28 reserved-event-names ownership.** VT-28 shipped `*_shell` events with `status: skipped_schema_pending`. VT-175 introduces the REAL `attribution_closed` / `day39_continue` / `day39_refund_triggered` events. **The event schemas are registered in this PR**, but **no production emission happens here** — VT-176 wires `scheduled_triggers.py` to invoke the new evaluators. The deterministic-trigger CI gate keeps Pillar 1 honest. Documented in event_schemas.py comment block.

3. **Pillar 1 enforcement scope.** The existing `gate-no-llm-in-deterministic-triggers` CI gate scans `scheduled_triggers.py` function bodies. The NEW `billing/day39_evaluator.py` + `billing/attribution_close.py` modules need similar protection. **Q2 below — extend the gate to also scan these two new modules' bodies?** Same 3-line grep pattern.

4. **Idempotency invariant under concurrent close.** Two simultaneous `close_attribution(campaign_id)` callers could both read `attribution_closed_at IS NULL` then both UPDATE. Mitigation: use `UPDATE campaigns SET total_arrr_paise=..., attribution_closed_at=now() WHERE id=$1 AND attribution_closed_at IS NULL RETURNING 1` — first writer wins; second gets 0-row-update + reads back as `already_closed`. Atomic per row. No advisory lock needed.

5. **Evaluator integer-division precision.** ARRR vs cumulative-fees comparison: 2x. Doing `arrr_paise >= 2 * cumulative_fees_paise` in Python with BIGINT inputs from SQL → no precision loss. SQL aggregation uses `SUM(attributed_paise)::BIGINT` to keep the integer pipeline.

6. **Migration DDL transaction safety.** `CREATE TABLE` + `ALTER TABLE ... ADD COLUMN` + `CREATE INDEX` are all transactional in Postgres. The migration is one BEGIN/COMMIT block; failure rolls back atomically. No partial state.

7. **5-canary regression sweep budget.** VT-28 + VT-171 + VT-104 + VT-103 + VT-102 sequential ≈ 26+26+25+50+47 = ~175s. Within VT-175 canary's ≤60s budget for the canary itself, plus regression overhead is captured in pre-merge-result audit (post-canary sweep).

## Plan-ready questions for Cowork

### Q1 — RLS GUC convention

The brief's example uses `current_setting('app.tenant_id')::uuid`. Migration 000b_rls_helpers.sql defines `app_current_tenant()` reading `app.current_tenant`. Every existing table (campaigns, tenants, pipeline_log, …) uses `app_current_tenant()`. `tenant_connection()` Python wrapper at `src/orchestrator/db/tenant_connection.py` does `SELECT set_config('app.current_tenant', %s, false)`.

**Recommend:** use `app_current_tenant()` for the attributions RLS policy. Matches every other table; works with the existing connection wrapper. CL-82's spirit (RLS via session GUC) is honoured; just the helper-function form. Brief's example was likely paraphrased.

Alternative: introduce a NEW GUC `app.tenant_id` parallel to `app.current_tenant`. Adds a fork in the substrate — not recommended.

### Q2 — Extend `gate-no-llm-in-deterministic-triggers` to billing/ modules?

VT-28 shipped a CI gate scanning function bodies in `scheduled_triggers.py`. The new `billing/day39_evaluator.py` + `billing/attribution_close.py` are deterministic by spec; they're not in the gate's scan scope today.

**Recommend YES** — extend the gate to also scan `apps/team-orchestrator/src/orchestrator/billing/*.py` files for forbidden tokens (`ChatAnthropic | Anthropic | claude- | langchain_anthropic | orchestrator_agent | supervisor | messages\.create`). Adds ~3 lines to the gate. Pays for itself — Pillar 1 enforcement at code level, complementing the canary's runtime assertion #5 + #8.

Alternative: rely on canary runtime check + code review. Less robust; structural enforcement beats per-PR vigilance per the principle that motivated the VT-28 gate in the first place.

## Status

`.viabe/queue/VT-175/status` flipped `queued` → `planning` → `review`. Signalling plan-ready. Will proceed on APPROVED.
