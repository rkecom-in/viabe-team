---
reviewer: cowork
verdict: APPROVED-with-conditions
ts: 2026-05-26T01:55:00+05:30
plan_sha: (queue/VT-103/plan.md)
---

# Review — VT-103 plan

**APPROVED with three narrow conditions.** Plan is sound: scope-trim taken cleanly (Telegram bot + cron + VT-35 ceiling integration all deferred with function-as-tool boundaries), ARRR sourcing surfaced as a real question with a sensible fallback, materialized view ⊕ RLS tension handled correctly (MV is service-role only; tenant queries hit raw `pipeline_log` events directly).

## What I like

- **Scope-trim baked in.** §1, §2, §5 ship; Telegram bot wiring + cron + VT-35 integration explicitly deferred. The `format_cost_breakdown_for_ops()` function gives the future VT-30 bot PR a clean drop-in.
- **Materialized view + RLS handled correctly (risk #3).** MV is service-role-only; `get_tenant_cost` uses real `tenant_connection()` against raw events; `get_workspace_cost_summary` uses MV for fast path + raw for sub-hour. Tenant isolation never crosses the MV.
- **Brief-decay corrections pre-acked (risk #4).** Migration `022` (not `039`), paths under `apps/team-orchestrator/`, PR title `(VT-103)`, target `main`, reviewers retired. No-op confirmations.
- **Anomaly detection floor + new-tenant handling (risk #6).** Acknowledges the edge case before I had to surface it.
- **Rule #15 audit standard internalised (risk #7).** Per-assertion observed dicts/lists captured from the start. No "summary first then bounce" cycle.
- **Estimate honest at 165K/180K ceiling.** Split contingency named (PR-A core / PR-B integration tests).

## Conditions (must address before pr-ready, but doesn't block implementation start)

### Condition 1 — ARRR sourcing: ship plan-tier approximation; document the limitation in code

Risk #1 surfaces a real gap. The brief assumed `campaigns` table has amount columns; the actual `016_campaigns.sql` schema doesn't. CC's fallback (env-driven `<PLAN>_PRICE_PAISE × tenants.plan_tier`) is the right ship-thin call.

**Approve the env-driven approximation. Add to the docstring of `get_tenant_unit_economics`:**

> *"ARRR is computed from `tenants.plan_tier × <PLAN>_PRICE_PAISE` from env config — this approximates monthly subscription revenue, not realised revenue. Day-39 calibration (VT-92 evaluator) using this ratio is acceptable for plan-fit signals but NOT for actual refund-amount calculation. When real revenue events ship (`payment_event` payloads carrying `amount_paise` from VT-89 Razorpay wiring), this function should switch to sum-of-payment-events scoped to (since, until). Tracked as a known limitation."*

That's the documentation; no code change beyond docstring.

### Condition 2 — Anomaly detection: confirm baseline-relative + new-tenant floor + minimum absolute window floor

Risk #6 surfaces the floor question. Confirm:

- **Baseline-relative is the default signal** (`window_avg / baseline_avg >= multiplier`).
- **New-tenant floor:** if `baseline_avg == 0` (no 28-day history; tenant joined within last 28 days), do NOT flag — return the tenant as "ineligible_new_tenant" with a marker, not as anomalous. Otherwise pilot will be all-tenants-flagged for the first month.
- **Minimum absolute window cost:** even if baseline-relative says "flag," suppress if `window_total < 10000` paise (₹100). Filters spurious flags for tenants with near-zero baseline + a tiny absolute spike (e.g., baseline ₹2/day, window jumps to ₹6/day → 3× ratio but only ₹42 absolute — not actionable). Make this a constant (`_ANOMALY_MIN_WINDOW_PAISE = 10_000`) so it's discoverable + adjustable.

Same `_RUNAWAY_MIN_WINDOW_PAISE` floor for `runaway_alert_candidates` (suppress tenants whose absolute spend is too small to investigate even if it crosses the plan-pct threshold).

### Condition 3 — `cost_category` field convention documented in `event_schemas.py`

Risk #2 — CC already plans this. Confirming: document `cost_category` as an OPTIONAL convention in the `external_api_call` event schema. Values: `llm` / `twilio` / `razorpay` / `apify` / `infra_allocated` / (fallback to `other`). Aggregator falls back to bucket-by-vendor when absent. Future per-feature wiring populates the field at write-time.

The docstring + schema comment is the deliverable; no enforcement (per VT-102's soft-validation pattern).

## Out of scope (Cowork concurs)

- Real Telegram `/costs` dispatch (VT-30 dependency; ship `format_cost_breakdown_for_ops` as the seam)
- Real scheduled-trigger cron (VT-28 dependency; ship `runaway_alert_candidates` as callable)
- Full VT-35 integration test (1-line stub assertion is enough; full test in VT-35 row)
- Switching ARRR to payment-event sum (deferred until Razorpay wiring lands; condition 1 docstring captures the limitation)
- Computing cost-from-rates from `model_pricing.yaml` (per-feature wiring; brief says source events ship `cost_paise` already populated)

## Single-PR strong preference

165K estimate vs 180K ceiling. If implementation drifts past 180K, do the split CC named (PR-A core ship + PR-B integration tests). Surface via `plan-updated` signal mid-flight if you see the burn — don't push past silently.

## Authority

Flip `.viabe/queue/VT-103/status` from `review` → `implementing` and proceed. Canary spec is already in the brief's §Canary section. Run canary as part of impl; `pre-merge-result` signal MUST include the verbatim per-assertion audit artifact (lesson from VT-102).

**Pillar 7:** merge requires Fazal `type: task` with `authorized_by: fazal`. The "Fazal personally signs off on thresholds" line in the brief is satisfied by the type:task itself (the thresholds = the ones in conditions 1-2 above).

Go.
