---
task: VT-176
author: claudecode
ts: 2026-05-26T14:58:00+05:30
estimated_tokens: 85000
estimated_minutes: 80
classification: critical-path-feature-completion
follows: VT-175
---

## TL;DR

Replace 3 shell trigger bodies in `scheduled_triggers.py` with real implementations that fan-out to VT-175's `billing/` modules + `apply_transition` for the day-39 refund branch. Weekly cadence stays plumbing-mode (CL-274). Update tests + ship new VT-176 canary alongside (the VT-28 canary stays as-is to lock VT-28's PR audit trail). 6-canary regression sweep. Single PR ~85K.

## Approach

### 1. Signature preservation

VT-28's body signatures are `run_*_body(now: datetime | None = None)`. The brief's sample shows `(campaign_id, conn)` / `(tenant_id, conn)` — but VT-175's billing modules don't take `conn` (they `get_pool().connection()` internally). **Keep VT-28's `now=None` signature.** Bodies scan eligibility internally + fan out to per-campaign / per-tenant billing calls. Canary calls with synthetic `now` for determinism.

### 2. `run_attribution_close_body(now=None)` — REAL

- Scan campaigns where `attribution_close_at <= now AND attribution_closed_at IS NULL AND status='sent'` (uses VT-175's new columns).
- For each: call `close_attribution(campaign_id)` from `orchestrator.billing` — already emits `attribution_closed` event with `total_arrr_paise` + `attribution_row_count`.
- Body itself emits NO additional event (the billing module owns the emission).
- Returns `list[AttributionCloseResult]` for canary inspection.

### 3. `run_day39_evaluation_body(now=None)` — REAL

- Scan tenants where `paid_conversion_at + 39 days <= now AND phase IN ('paid_active', 'paid_at_risk')` AND no prior `day39_*` event for that tenant.
- For each: call `evaluate_day39(tenant_id)` from `orchestrator.billing` — emits `day39_continue` or `day39_refund_triggered`.
- **Refund branch:** call `transitions.apply_transition(tenant_id, to_phase='refunded', reason='day39_refund')` — Pillar 1 + CL-104 (apply_transition is the SOLE public phase mutator).
- Returns `list[Day39Verdict]`.

### 4. `run_monthly_impact_body(now=None)` — partial real

VT-9.6 PDF generator is not done. Body emits `monthly_impact_started` event for each `paid_active` subscriber whose `paid_conversion_at` is > 30 days ago. Downstream PDF flow ships in VT-9.6 successor.

- Scan tenants where `phase='paid_active' AND paid_conversion_at <= now - 30 days`.
- For each: emit `monthly_impact_started` event via `log_event` with the canonical schema.
- Returns `list[UUID]` of tenants notified.

### 5. Event schemas

`attribution_closed`, `day39_continue`, `day39_refund_triggered` already registered by VT-175. Add `monthly_impact_started` (NEW). Keep `*_shell` schemas registered (audit-trail integrity for historical rows; CL-176 Rule #9).

### 6. Update existing tests

`tests/orchestrator/test_scheduled_triggers.py` currently asserts shell event names. Replace assertions to assert real event names. Add fan-out tests (canary covers this end-to-end, unit tests focus on per-tenant call delegation via monkeypatch).

### 7. NEW canary `canaries/vt176_real_trigger_bodies.py`

10 assertions across 5 groups per brief §Canary. Real Supabase. Anthropic env loaded ONLY for Group D weekly cadence. Group C asserts REAL event types in pipeline_log (NOT `*_shell`). Group E: runtime grep-pattern verification of body functions.

VT-28's canary (`canaries/vt28_scheduled_triggers.py`) stays intact — locks VT-28's PR audit trail. It now passes for a different reason: the VT-28 canary tested the OLD shell behavior, but `run_attribution_close_body` now scans + delegates. The VT-28 canary's Group C assertions checked that shell events landed — those events won't be emitted by the new bodies unless eligible candidates exist. **Risk surfaced as Q1 below.**

### 8. apply_transition wiring

`from orchestrator.transitions import apply_transition`. Gate-no-llm-in-deterministic-triggers' forbidden tokens don't include `transitions` (correct — apply_transition is deterministic; CL-104). One-line call in day-39 refund branch.

## File changes

- **MODIFY** `src/orchestrator/scheduled_triggers.py` — replace 3 shell bodies + add eligibility-scanner helpers
- **MODIFY** `src/orchestrator/observability/event_schemas.py` — register `monthly_impact_started`
- **MODIFY** `tests/orchestrator/test_scheduled_triggers.py` — flip shell-event assertions to real-event assertions
- **NEW** `apps/team-orchestrator/canaries/vt176_real_trigger_bodies.py` — 10 assertions
- Optional: **KEEP-OR-MIGRATE** `canaries/vt28_scheduled_triggers.py` — covered by Q1

## Test plan

- `pytest tests/orchestrator/test_scheduled_triggers.py` — updated tests pass
- `pytest tests/` orchestrator-wide — zero regression
- VT-176 canary 10/10 PASS against real backends
- 6-canary regression sweep: VT-102 7/7 + VT-103 8/8 + VT-104 10/10 + VT-171 11/11 + VT-28 (see Q1) + VT-175 8/8

## Risks

1. **VT-28 canary's Group C assertions check shell event types** — after VT-176 changes the body emissions to real event types, the VT-28 canary's assertions #6/#7/#8 will FAIL. **Q1 below — keep-and-update vs delete-and-supersede.**

2. **Fan-out under eligibility-empty case.** If no campaigns are eligible for close, `run_attribution_close_body` runs through the scan + emits nothing. Same for day-39 + monthly impact. Bodies become observability-quiet on empty days. Canary seeds eligibility first; production runs find/don't-find naturally. No regression for the observability stack.

3. **`apply_transition` requires DBOS workflow context?** Need to verify `transitions.apply_transition` works under the scheduled-handler call path (which runs inside `@DBOS.scheduled` so DBOS context is present). Local pre-canary verification will surface any context dependency.

4. **Monthly impact 30-day eligibility filter.** Brief says skip subscribers with `paid_conversion_at < 30 days ago`. Implementation: `paid_conversion_at <= now - 30 days`. Test fixture seeds tenants with appropriate `paid_conversion_at` values.

5. **6-canary regression sweep budget.** ~26+50+25+26+27+32 = ~186s sequential. Within budget — pre-merge-result audit, not canary wall-clock.

6. **gate-no-llm-in-deterministic-triggers** must stay green. Body bodies now import from `orchestrator.transitions` (allowed; deterministic) + `orchestrator.billing` (allowed; in gate scope and verified zero-LLM by gate itself). Forbidden tokens unchanged.

## Plan-ready questions

### Q1 — VT-28 canary disposition

After VT-176 swaps shell bodies for real bodies, VT-28 canary's Group C assertions #6/#7/#8 break (they check for `attribution_close_shell` / `day39_shell` / `monthly_impact_shell` event types in pipeline_log; new bodies emit `attribution_closed` / `day39_continue|refund_triggered` / `monthly_impact_started`).

**Options:**
- **(A) Recommend — delete `canaries/vt28_scheduled_triggers.py`** in this PR. VT-176 canary supersedes; the trigger registration + workflow_id determinism + cron table assertions all migrate to VT-176's Groups A/B. VT-28's PR audit trail is locked in `pipeline_log` rows from the original canary run + the merged commit on main. Deleting the file forward-loses no evidence.
- **(B)** Keep VT-28 canary but mark it skipped post-VT-176 with a header note. Adds a stale-looking file to the repo; reviewer noise.

**Recommend (A).** Lossless evidence-wise; cleaner repo.

### Q2 — 30-day monthly-impact eligibility window vs broader paid window

Brief says skip subscribers with `paid_conversion_at < 30 days ago`. The trigger fires monthly (1st of month). For the canary to test the body deterministically, the test fixture seeds a tenant with `paid_conversion_at = now - 45 days`. **Confirming: 30-day threshold is the line.** No alternative needed.

## Status

`.viabe/queue/VT-176/status` flipped `queued` → `planning` → `review`. Signalling plan-ready. Will proceed on APPROVED.
