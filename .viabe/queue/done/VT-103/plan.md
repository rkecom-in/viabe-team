---
task: VT-103
author: claudecode
ts: 2026-05-26T01:47:37+05:30
estimated_tokens: 165000
estimated_minutes: 130
---

## Approach

Build the cost dashboard as a single seam on top of VT-102's `pipeline_log`: migration `022_tenant_cost_views.sql` (next numeric — brief said `039`; same Notion-projection gap as VT-101/102) for the materialized view + supporting indexes, `observability/cost_dashboard.py` for the five aggregation functions, `observability/types.py` extended with `TenantCostBreakdown` / `WorkspaceCostSummary` / `TenantUnitEconomics` dataclasses, `config/model_pricing.yaml` for vendor pricing pinned by `effective_at`. Cost sourcing is read-only out of `pipeline_log` event payloads' `cost_paise` field — VT-103 doesn't write to pipeline_log; that's per-feature wiring elsewhere. The aggregator opens service-role connections directly (`get_pool().connection()`) for workspace queries (top-10, anomaly detection) and `tenant_connection()` for tenant-bound `get_tenant_cost` queries — RLS does the isolation work for the latter.

Scope-trim per Cowork's heads-up §"Scope-trim conversation expected": ship aggregation (§1, §2, §5), unit-economics (§5), anomaly detection function, cost-runaway threshold function, plus tests + canary. **Defer** real Telegram bot wiring (VT-30 dependency) and real cron triggers (VT-28 dependency) — both ship as pure callable functions with clean signatures so the future wiring PRs are trivial. **Defer** VT-4.4 (renumbered VT-35 per CL-244) integration test to a 1-line assertion that calls the existing `agent/limits/` cost-cap check with a synthetic `cost_paise > 5000` value; full integration test goes in the VT-35 row, not here.

## File changes

- **NEW `migrations/022_tenant_cost_views.sql`** — `CREATE MATERIALIZED VIEW tenant_cost_daily AS SELECT tenant_id, DATE(created_at) AS day, event_type, (payload->>'cost_category') AS category, SUM((payload->>'cost_paise')::BIGINT) AS cost_paise, COUNT(*) AS event_count FROM pipeline_log WHERE event_type='external_api_call' AND payload?'cost_paise' GROUP BY 1,2,3,4`. Indexes on `(tenant_id, day DESC)` + `(day DESC, cost_paise DESC)`. `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a UNIQUE index — add one on `(tenant_id, day, event_type, category)`. View is service-role-only (no GRANT to app_role; matches workspace concern).

- **NEW `apps/team-orchestrator/config/model_pricing.yaml`** — versioned per-vendor pricing. Top-level: `effective_at: 2026-05-26`, then `llm.anthropic.{claude-sonnet-4-6, claude-opus-4-7}.{input_per_1m_paise, output_per_1m_paise}`, `twilio.whatsapp_per_message_paise`, `razorpay.mdr_basis_points` (200 = 2%), `apify.<actor>.per_run_paise`, `resend.per_email_paise` (0 — free tier Phase 1). Loaded via `yaml.safe_load`. Tests assert key shape + presence; computing-cost-from-rates is per-feature wiring (not VT-103 scope — brief moves that to "the source events ship a `cost_paise` payload field").

- **NEW `apps/team-orchestrator/src/orchestrator/observability/cost_dashboard.py`** —
  - `get_tenant_cost(tenant_id, since, until) -> TenantCostBreakdown` — sums `(payload->>'cost_paise')::BIGINT` from `pipeline_log` for events where `event_type='external_api_call'`, scoped by `tenant_connection(tenant_id)` (RLS-enforced). Breakdown by `payload->>'cost_category'` (`llm`, `twilio`, `razorpay`, `apify`, `infra_allocated`); unknown categories bucket into `other`.
  - `get_workspace_cost_summary(since, until, top_n=10) -> WorkspaceCostSummary` — service-role; returns top-N tenants by total cost descending. Uses the `tenant_cost_daily` MV when available + falls back to raw events for the sub-hour window.
  - `get_tenant_unit_economics(tenant_id, since, until) -> TenantUnitEconomics` — `arrr_paise / cost_paise` ratio. **Open question #1 below**: ARRR sourced from `tenants.plan_tier` × `<PLAN>_PRICE_PAISE` env (per `.env.example`'s FOUNDING/STANDARD/PRO), not from `campaigns` table — brief assumed `campaigns` had amount columns, but the schema in `016_campaigns.sql` has none. Will use env-driven plan pricing unless review pushes otherwise.
  - `detect_cost_anomalies(reference_days=28, window_days=7, multiplier=2.0) -> list[CostAnomaly]` — flags tenants whose 7-day cost > 2× their 28-day baseline. Pure window comparison, no statistical fit.
  - `runaway_alert_candidates(plan_pct_threshold=0.5) -> list[CostRunaway]` — tenants whose 7-day spend exceeds `plan_pct_threshold` × monthly plan fee. Callable function; cron wiring deferred.
  - `format_cost_breakdown_for_ops(breakdown: TenantCostBreakdown) -> str` — markdown-formatted block ready to drop into a Telegram alert or PR comment. **No Telegram dispatch here** — the bot wiring lives in the future VT-30 PR; this function is the function-as-tool boundary Cowork pre-approved.

- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/types.py`** — append `TenantCostBreakdown` (tenant_id, since, until, total_paise, by_category dict, event_count), `WorkspaceCostSummary` (since, until, top_tenants list[(tenant_id, paise)], workspace_total), `TenantUnitEconomics` (tenant_id, arrr_paise, cost_paise, ratio), `CostAnomaly` (tenant_id, reference_avg_per_day, window_avg_per_day, multiplier_observed), `CostRunaway` (tenant_id, window_cost_paise, plan_monthly_paise, pct_observed). All frozen dataclasses.

- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/__init__.py`** — re-export the five aggregation functions + five new dataclasses.

- **NEW `apps/team-orchestrator/tests/orchestrator/observability/test_cost_dashboard.py`** — pytest. ~10 pure tests (model_pricing.yaml shape + parse, breakdown summing math, anomaly threshold logic, runaway threshold logic, plan_tier → monthly_paise mapping). 6 integration-gated tests covering the brief's 8 test bullets (cross-tenant isolation, materialized-view refresh, top-10 ranking, anomaly detection end-to-end, hard-ceiling stub, ARRR ratio).

- **NEW `apps/team-orchestrator/canaries/vt103_cost_dashboard.py`** — Rule-#15 canary, 8 assertions verbatim from brief §Canary. Subshell-source `supabase-dev.env`. PREFLIGHT echoes resolved host. Per-assertion captures: observed `TenantCostBreakdown` dicts, top-10 ranked list, cross-tenant counts, anomaly verdict. Audit artifact: top-20 inserted canary rows + final `cost_dashboard` outputs as JSON. Best-effort cleanup of `component='canary'` rows at exit.

## Test plan

Pure unit tests run unconditionally (yaml parse, math). Integration tests gated on `RUN_INTEGRATION_TESTS=1`; uses canary-prefixed UUIDs to avoid clobbering other test data. Pre-PR locally:
- `pytest tests/orchestrator/observability/ -q` — full suite passes; new tests added.
- `ruff check apps/team-orchestrator migrations` — clean.
- `mypy --strict src/orchestrator/observability/` — clean.
- Apply `022_tenant_cost_views.sql` to dev DB, run canary, expect 8/8 PASS. Verbatim audit artifact in `pre-merge-result-supplement`.

## Risks

1. **ARRR sourcing (open question).** Brief assumes `campaigns` table has amount columns; current `016_campaigns.sql` schema doesn't (just `template_id`, `body_params` JSONB, `status`). Plan defaults to env-driven plan-tier mapping — `tenants.plan_tier` → `FOUNDING_PRICE_PAISE=249900` / `STANDARD_PRICE_PAISE=499900` / `PRO_PRICE_PAISE=1499900` from `.env.example`. Surfacing as plan-ready question — if Cowork wants a different ARRR source (e.g., the `payment_event` events in pipeline_log carry `amount_paise`), I'll wire that instead. Either way the canary's assertion #7 ratio is the test.

2. **`cost_category` field is invented by VT-103.** The brief assumes `external_api_call` payloads carry `cost_category` (`llm` / `twilio` / `razorpay` / `apify` / `infra_allocated`). VT-102's `event_schemas.py` doesn't require it. The aggregator falls back to bucket-by-`vendor` when `cost_category` is absent — keeps backward compatibility. I'll document the expected payload field in `event_schemas.py` as an optional convention; per-feature wiring (future PRs) populates it.

3. **Materialized view ⊕ RLS.** Postgres materialized views don't enforce RLS the way regular tables do — the view's data is computed under the role that ran `REFRESH MATERIALIZED VIEW`. To keep tenant isolation: I will NOT grant the view to `app_role`. Workspace queries (top-10, anomaly) run under service role only. `get_tenant_cost` uses `tenant_connection()` + queries `pipeline_log` directly (real RLS, MV bypassed for that path). The MV is only for the workspace-level fast queries — never for tenant-bound reads.

4. **Brief decay (same class as VT-101/102).** Path corrections (`apps/team/` → `apps/team-orchestrator/`), migration number (`039` → `022`), PR title `(VT-Observability-Cost)` → `(VT-103)`, merge target `main` not `dev`, retired reviewers skipped, ARRR sourcing per risk #1.

5. **Estimate vs ceiling.** 165K / 130 min vs your 180K ceiling. Tight. If implementation crosses 180K I'll surface via `plan-updated` and propose split: PR-A = migration + types + aggregation + pure tests + canary; PR-B = integration tests + runner-side `cost_paise` payload emit hooks (which the brief calls out as belonging to per-feature work, so the split would just defer the integration suite).

6. **Anomaly detection definition.** Brief uses "7-day cost > 2× 28-day baseline" — implemented as `(sum(last_7_days) / 7) / (sum(days_-28_to_-8) / 21) >= 2.0`, with floor for tenants whose 28-day baseline is zero (treated as "new tenant, not yet flaggable"). Surfacing the floor explicitly — alternative is to flag any tenant whose 7-day cost > some absolute floor regardless of baseline.

7. **Rule-#15 audit standard.** Cowork's question on VT-102 set the standard: verbatim per-assertion evidence in `pre-merge-result-supplement`. VT-103's canary script will print per-assertion observed dicts/lists from the start (no "summary first then bounce" cycle).
