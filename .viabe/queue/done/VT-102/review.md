---
reviewer: cowork
verdict: APPROVED-with-conditions
ts: 2026-05-26T00:00:00+05:30
plan_sha: (queue/VT-102/plan.md)
---

# Review — VT-102 plan

**APPROVED with two narrow conditions.** Plan is thorough, all 8 risks named explicitly, canary spec matches the brief's §Rule #15 section verbatim, brief-decay corrections (migration `021` not `038`; `(VT-102)` not `(VT-Observability-Cost)`; `main` not `dev`; CoderX retired) pre-acked.

## What I like

- **Migration number correction surfaced (risk #1).** Same class as VT-101's `apps/team/` issue. You handled it inline rather than escalating — correct call.
- **Reuses `observability/pii.py` via `redact_for_log` alias (Cowork-recommended Option A).** Future VT-104 swaps in one place.
- **Async DBOS interop branching (risk #3).** Detecting running loop and switching strategy is exactly right; the orchestrator's hot path is mostly inside `@DBOS.step` bodies.
- **Service-role-only retention sweep with belt-and-suspenders check.** RLS bypass + explicit role check = a misconfigured caller can't silently no-op the sweep.
- **Canary cleanup uses identifiable canary-prefixed UUIDs + falls back to 90-day retention.** Robust without being brittle.
- **Soft schema validation writes the `payload_validation_failed` flag in payload.** Matches brief; preserves observability under code drift.
- **PR-split contingency named.** If budget burns past 180K mid-implementation, split into "core" + "integration tests" — single-PR strong preference, split is escape hatch.

## Conditions (must address before pr-ready)

### Condition 1 — Workspace-level (tenant_id NULL) INSERT must be explicitly tested

The plan says "writer opens its own connection via `tenant_connection(tenant_id)` if tenant_id else service connection." That means tenant_id=NULL writes go through service role, which bypasses RLS. Good.

But the integration test suite as planned (suite 4 "Cross-tenant" + suite 5 "Workspace-level visibility") only verifies the SELECT side for NULL rows. It doesn't verify the INSERT side. Add:

> **Suite 5a (new):** Service-role connection inserts a row with tenant_id=NULL. Direct SQL SELECT under service role returns 1 row. Same SELECT under tenant_A app_role returns 0 rows. (Proves write path AND read path for workspace events.)

Without this, a regression where `tenant_id=NULL` writes fail silently (e.g., a future RLS policy refactor that breaks service-role INSERT) could ship.

### Condition 2 — Async DBOS interop branching needs a pytest test for both paths

Risk #3's mitigation is real but only documented in a docstring. The runtime detection logic (`asyncio.get_running_loop()` try/except → schedule_task vs sync-wrapper-on-thread) is exactly the kind of thing that breaks silently when DBOS upgrades its event loop or when called from a non-async context that someone forgot was non-async.

Add to suite 2 (PII redaction):

> **Suite 2 additional cases:** Call `log_event` from inside a running event loop (`asyncio.run(...)` wrapping the call) — assert returns immediately, write happens on background task. Call `log_event` from a synchronous context (no loop running) — assert returns immediately, write happens on a thread. Both paths complete the actual INSERT (verified via direct SQL SELECT for run_id).

Pure-unit testing isn't enough here; this needs the integration-gated suite because the branching depends on actual loop detection.

## Out of scope for this PR (Cowork concurs — do NOT scope-creep)

- Wiring the nightly retention cron — Phase 2 / a follow-up VT row. Function lands here; scheduling lands elsewhere.
- `pipeline_log_failures` sentinel table — Phase 2 per brief.
- The other ~16 event types beyond the 14 enumerated — incremental per future feature briefs.
- Replacing `redact_for_log = redact_for_langsmith` alias with a domain-pure redactor — that's VT-104's scope, not this row's.

## Brief-decay corrections — pre-acknowledged

- Migration number: brief `038` → actual `021` ✓
- PR title: brief `(VT-Observability-Cost)` → actual `(VT-102)` (numeric VT-ID per CI regex) ✓
- Merge target: brief `dev` → actual `main` ✓
- Reviewers: brief CoderC/CoderX → retired per CL-151; CC self-reviews; canary IS the rigor ✓
- Paths: brief `apps/team/...` → actual `apps/team-orchestrator/...` ✓

## Budget

180K ceiling. CC estimates 170K. Tight. If you cross 180K mid-implementation, do the split CC named (core PR + integration-tests follow-up) — surface via a `plan-updated` signal, don't push past silently. The strong preference is single-PR for the same architectural reason as VT-101 (one logical seam), but the discipline is "split rather than skip a test."

## Authority + signal

Proceed to implementation. Flip `.viabe/queue/VT-102/status` from `review` → `implementing`. When PR opens, signal `pr-ready` as usual. The Rule #15 canary is already spec'd in the brief — run it as part of the implementation cycle (not a separate pre-merge-check signal request); send `pre-merge-result` with the captured audit JSON once canary completes.

**Pillar 7 unchanged:** the eventual `gh pr merge` still requires Fazal's explicit `type: task` with `authorized_by: fazal`. Don't merge.

Go.
