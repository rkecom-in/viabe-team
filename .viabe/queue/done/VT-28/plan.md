---
task: VT-28
author: claudecode
ts: 2026-05-26T04:08:00+05:30
estimated_tokens: ~165000  (depends on Q1 answer; up to 200K if schema migration included)
estimated_minutes: ~150
---

## TL;DR

The brief's deterministic-SQL trigger bodies assume a schema that doesn't exist on `main` post-VT-104 merge (`3593ce7`). 5 columns + 1 table missing. Plan can't ship the canary's Group C in its full form without first either (a) adding migration `023` for that schema (~+30K tokens, blast radius into L1/L2/billing/owner-surface), or (b) shipping the **trigger infrastructure only** (registration + workflow_id derivation + idempotency + pipeline_log integration + auto-resume) with the deterministic-SQL bodies stubbed as TODO + the canary's Group C trimmed to "no-LLM + pipeline_log row landed" rather than ARRR-math assertions.

Two plan-ready questions for Cowork below. Recommend **path (b)** — minimum blast radius, keeps single-PR preference, preserves all 5 canary groups (with Group C in trimmed form).

## Approach (sketch, conditional on Q1 answer)

`apps/team-orchestrator/src/orchestrator/scheduled_triggers.py` — single module with:

- `@DBOS.scheduled(cron=...)` decorator registration for the 4 cron cadences.
- 4 child `@DBOS.workflow` functions, each invoking 1+ `@DBOS.step` granular steps so DBOS auto-resume works at step boundary.
- workflow_id derivation: `weekly:{tenant_id}:{iso_week}`, `attribution_close:{campaign_id}`, `day39:{tenant_id}`, `monthly:{tenant_id}:{YYYY-MM}` — passed as `workflow_id=` kwarg to `DBOS.start_workflow()`, so DBOS exactly-once semantics short-circuit duplicate workflow_ids natively (no separate idempotency table).
- Each scheduled poller iterates candidate (tenant_id, …) and spawns child workflows; the child workflow body persists `pipeline_log` rows via `log_event` at every meaningful step (Pillar 8 — single observability sink).
- Weekly cadence body invokes the orchestrator-agent + supervisor pipeline (existing `build_orchestrator_agent` + `supervisor.run_supervisor` per VT-32/33-37/39); the agent call is wrapped in `@traceable_node` so LangSmith captures the trace.
- Attribution-close / day-39 / monthly-impact bodies are pure SQL + `log_event`. **NO orchestrator-agent import** in those code paths — keeps Pillar 1 separation enforced statically. CI gate (`gate-no-llm-in-deterministic-triggers`) — proposed below as Risk #4 mitigation.

Synthetic-clock injection for the canary: each `@DBOS.scheduled` decorator is registered around a *callable* function; the **callable itself accepts a `now: datetime | None = None`** kwarg. DBOS calls it with `now=None` (which defaults to `datetime.now(timezone.utc)`); the canary calls the same callable directly with `now=<synthetic IST Monday 9 AM>`. This is the same shape `dbos_purge.purge_workflow_inputs_scheduled` uses for testability.

## File changes (conditional on Q1)

### Path (b) — minimum (recommended)

- **NEW** `apps/team-orchestrator/src/orchestrator/scheduled_triggers.py` — 4 trigger registrations + 4 child workflow bodies. Deterministic bodies (attribution close, day-39, monthly impact) ship as **shells** that log `*_fired` events to pipeline_log but skip the real SQL aggregation (TODO comments calling out the schema gaps + the future VT row that will fill them). Weekly cadence body invokes orchestrator-agent (real LLM call).
- **NEW** `apps/team-orchestrator/tests/orchestrator/test_scheduled_triggers.py` — pure unit tests for workflow_id derivation, skip-condition predicates (the predicates are pure functions; the underlying SQL queries are TODO), and `@DBOS.scheduled` decoration smoke test.
- **NEW** `apps/team-orchestrator/canaries/vt28_scheduled_triggers.py` — 10 assertions:
  - **Group A** (3): real LangSmith trace from weekly cadence; pipeline_log rows for all 4 trigger types with redacted-only payloads; VT-104 byte-identical token format preserved.
  - **Group B** (2): DBOS workflow_id idempotency (same `(tenant_id, iso_week)` short-circuits); cross-trigger isolation.
  - **Group C** (3) — TRIMMED form: instead of ARRR-math, each deterministic trigger asserts (a) pipeline_log row landed with correct event_type, (b) Anthropic call counter == 0 for that trigger's wallclock window, (c) workflow_status row in DBOS sys-DB ended in SUCCESS state.
  - **Group D** (1): real Anthropic Haiku call for weekly cadence; cost < ₹1; redacted prompt; LangSmith trace exists.
  - **Group E** (1): synthesised mid-step `RuntimeError`; DBOS resume captured; final pipeline_log row count == 1.
- **MODIFY** `apps/team-orchestrator/main.py` (or wherever DBOS app lifespan lives) to register the new scheduled triggers via the existing `register_purge_scheduler()`-style indirection — keeps DBOSRegistry.compute_app_version stable for tests + admin paths.
- **MODIFY** `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py` — register the 4 new event types (`weekly_cadence_fired`, `attribution_closed`, `day39_evaluated`, `monthly_impact_started`) per VT-104's soft-validation pattern.
- **NEW** `docs/team/scheduled-triggers.md` — cron + workflow_id + reasoning vs deterministic classification documented at the architecture level.

### Path (a) — full (only if Q1 says ship schema in this row)

All of (b), PLUS:
- **NEW** `migrations/023_attributions_and_cadence_columns.sql` — creates `attributions` table (id, tenant_id, campaign_id, subscriber_id, amount_paise, occurred_at, recorded_at) with RLS + indexes; adds `paid_conversion_at TIMESTAMPTZ` to `tenants`; adds `attribution_close_at`, `attribution_closed_at`, `total_arrr_paise BIGINT` to `campaigns`. 4 new RLS-bound tables/columns.
- Group C assertions expand to verify the ARRR aggregation math + day-39 phase transition end-to-end.
- Token cost: ~+30-40K (migration design + RLS + indexes + canary seed/cleanup of `attributions` rows + apply_transition wiring for the day-39 refund branch).

## Test plan

- `pytest tests/orchestrator/test_scheduled_triggers.py` — pure unit suite. Workflow_id derivation, predicate fns.
- `pytest tests/` orchestrator-wide — confirm zero regression from observability stack changes.
- Run VT-28 canary against Supabase dev pooler + Anthropic Haiku — 10/10 PASS, ≤ 90s, cost < ₹1.
- Run VT-101 + VT-102 + VT-104 canaries post-merge — 7/7 + 7/7 + 10/10 PASS unchanged (Condition-2 standard from VT-104 carried forward).
- Capture wall-clock + per-assertion observed + audit JSON top-5 rows for pre-merge-result supplement.

## Risks

1. **Schema gaps (the load-bearing risk).** Covered in Q1 below.

2. **DBOS testing harness vs synthetic clock.** DBOS's `@DBOS.scheduled` triggers fire by real cron; no documented `fire_now()` API. Canary uses the "extract callable, invoke with synthetic `now`" pattern — same shape as `dbos_purge.purge_workflow_inputs_scheduled`. If DBOS testing harness has gained a clock-injection facility post-2026-02 (Context7 may have newer docs than my training), prefer that. **Will surface as a question via Context7 lookup during implementation if the callable pattern hits a corner.**

3. **Weekly cadence orchestrator-agent invocation depth.** The full multi-agent state graph (orchestrator → supervisor → sales_recovery → collapse → terminal) is non-trivial to invoke from the trigger context, since it needs an `AgentState`, a checkpointer, tenant context (L0+L1-L4 bundle), and the spawn-tool wiring. **Plan:** the trigger's weekly cadence body builds a minimal `AgentState` with just `(tenant_id, run_id, trigger_reason='weekly_cadence')` + uses the existing `supervisor.run_supervisor` factory + relies on collapse_node + orchestrator_terminal to handle the no-spawn case (no campaign data → orchestrator-agent likely answers "insufficient context" + emits a refusal). That still produces a real Anthropic call + a LangSmith trace + the canary's Group D assertion fires. Going deeper (real campaign proposal) requires tenant context infrastructure that doesn't exist yet.

4. **Pillar 1 enforcement — no LLM in deterministic paths.** Easy to accidentally `import` the orchestrator-agent from `scheduled_triggers.py` and have it transitively pull a `ChatAnthropic` instance into the deterministic body. **Mitigation:** propose a small CI gate `gate-no-llm-in-deterministic-triggers` that greps the source of `close_attribution`, `day39_evaluation`, `monthly_impact_report` functions for any reference to `ChatAnthropic`, `Anthropic`, `claude-`, `langchain_anthropic`, `orchestrator_agent`, `supervisor`. Three-line gate, structural. Adds 5K tokens; pays for itself with Pillar-1 enforcement. **Q3 below — proposing this; awaiting your nod or veto.**

5. **180K ceiling.** Path (b) estimate ~165K (canary alone ~30K with all 10 assertions + DBOS substrate + clock injection). Path (a) ~200K and likely needs split. **If Path (b) approved, single PR target.** If Path (a) approved, split: PR-A = migration + trigger registration + deterministic-trigger bodies + their canary subset (Group A + B + C + E); PR-B = weekly cadence + orchestrator-agent invocation + canary Group D.

6. **Brief decay — same class as VT-101/102/103/104.** PR title `(VT-Orchestrator)` → `(VT-28)`, merge target `dev` → `main`, retired reviewers skipped. Mechanical fix.

7. **Out-of-scope rows referenced as deps in the brief body (VT-10.4 day-39 evaluator, VT-10.5 refund execution, VT-9.6 monthly impact PDF, VT-9.5 refund conversation, VT-9.3 weekly approval UX):** ALL Backlog. Trigger bodies handle this by emitting events (`day39_refund_triggered`, `monthly_impact_started`) that downstream PRs will consume; this PR doesn't wire those downstream flows. Documented in `docs/team/scheduled-triggers.md`.

8. **"Too clean to be true" carry-forward from VT-104.** If the canary passes 10/10 first run with zero changes — re-verify VT-101/102/104 byte-identical canary output BEFORE signalling pre-merge-result. VT-104 caught 3 real bugs; pattern continues.

## Plan-ready questions for Cowork

### Q1 — Schema migration scope (load-bearing)

The brief assumes columns + table that don't exist on `main`:

| Missing | Used by trigger |
|---|---|
| `attributions` table (id, tenant_id, campaign_id, amount_paise, occurred_at, ...) | attribution-close ARRR aggregation |
| `campaigns.attribution_close_at` / `attribution_closed_at` / `total_arrr_paise` | attribution-close window + final flag |
| `tenants.paid_conversion_at` | day-39 window calculation |
| (VT-Billing VT-10.4 day-39 evaluator) | day-39 ARRR-vs-fees branch |

**Path (a) — ship migration `023` in THIS row:** ~+30-40K tokens. Adds blast radius into L1/L2 (which references `attributions` conceptually), billing (VT-10.4 evaluator interface), owner-surface (monthly impact data prep). Single PR likely splits.

**Path (b) — ship trigger INFRASTRUCTURE only (recommended):** deterministic trigger bodies are SHELLS that log `*_fired` events but skip the SQL aggregation. Schema migration deferred to dedicated VT row (`VT-NEXT-attributions-schema` or similar). Group C canary assertions trimmed to "no-LLM + pipeline_log row + DBOS workflow_status SUCCESS" instead of ARRR math + phase transition. Single PR ships in ~165K. Subsequent VT row fills the bodies once schema lands.

**Recommend (b).** Same logic as VT-104 + customers-table: schema impact is too broad to bundle into a feature row; deferring lets the next dedicated row design the tables right.

### Q2 — Weekly cadence depth

Per Risk #3, the full orchestrator → supervisor → sales_recovery state graph needs an `AgentState`, a checkpointer (PostgresSaver from VT-3.x), and a tenant context bundle that isn't fully wired yet for ad-hoc invocation from a scheduled trigger.

**Option A — minimal invocation (recommend):** weekly cadence body invokes the orchestrator-agent DIRECTLY (skipping the supervisor's multi-agent dispatch), passing it a minimal `(tenant_id, run_id, trigger_reason='weekly_cadence')` context. This produces a real Anthropic call + a LangSmith trace + emits a `weekly_cadence_fired` event. Canary Group D's "real Anthropic call captured" passes. Subsequent VT row wires the full supervisor handoff once the rest of the context-bundle infrastructure (VT-126 L0 memory integration on Exec Order 10) lands.

**Option B — full supervisor invocation:** the trigger calls `supervisor.run_supervisor(initial_state)` directly. Requires building the full AgentState shape. Risk: brittle against unrelated state-graph changes; couples VT-28 to the entire multi-agent surface.

**Recommend A.** Minimal coupling, still produces the on-the-wire LLM proof the brief asks for.

### Q3 — Add `gate-no-llm-in-deterministic-triggers` CI gate?

3-line bash gate. Greps `close_attribution`, `day39_evaluation`, `monthly_impact_report` functions for `ChatAnthropic|Anthropic|claude-|langchain_anthropic|orchestrator_agent|supervisor`. Fails the build if any match. Pillar 1 structural enforcement.

**Recommend YES.** Pillar 1 is a Type 3 commitment; structural enforcement is cheaper than human review every PR.

## Status

`.viabe/queue/VT-28/status` flipped `queued` → `planning`. Signalling plan-ready. Awaiting your verdict on Q1/Q2/Q3; will proceed immediately on APPROVED.
