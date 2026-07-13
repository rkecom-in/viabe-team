---
plan_for: VT-431
title: Wire register_agent_coordinator() — G35 closure plan
status: READY-FOR-GATE
risk: activation (autonomous dispatch loop, downstream customer sends)
authored: 2026-06-29
---

# VT-431 — Coordinator wiring: ALREADY IMPLEMENTED (sprint row needs closure)

## Critical finding

**The code change is already committed.** Commit `d709532` ("feat(orchestration): wire
register_agent_coordinator() into main.py — activate the autonomous dispatch loop (VT-431,
dev-activation #1)") landed earlier in this branch and is present in `apps/team-orchestrator/src/main.py`
at lines 131–144. The sprint row at `.viabe/sprint/VT-431.md` reads `status: Queued` — that is stale
and needs to be closed to `Done`.

The Cowork gate needs to verify the DIFF (in d709532) before calling this closed.

---

## What was implemented (d709532 diff)

In `apps/team-orchestrator/src/main.py` lifespan, BEFORE `launch_dbos()`, the following block was
added after the `register_l2_send()` call:

```python
# VT-431: the autonomous agent-coordinator dispatch loop. Same
# register-before-launch contract — applies @DBOS.workflow to
# agent_dispatch_workflow + agent_coordinator_scheduled and @DBOS.scheduled
# (AGENT_COORDINATOR_CRON) to the sweep, so all three are in the DBOS
# registry when launch_dbos() computes the app_version hash and the daily
# sweep + DBOS recovery of an in-flight dispatch resolve.
from orchestrator.agents.coordinator import register_agent_coordinator

register_agent_coordinator()
```

`register_agent_coordinator()` (defined at `orchestrator/agents/coordinator.py:816`) does exactly
three things, idempotently:
1. `DBOS.workflow()(agent_dispatch_workflow)` — registers the per-item dispatch workflow
2. `DBOS.workflow()(agent_coordinator_scheduled)` — registers the sweep handler as a DBOS workflow
3. `DBOS.scheduled(AGENT_COORDINATOR_CRON)(agent_coordinator_scheduled)` — arms the daily cron

`AGENT_COORDINATOR_CRON = "0 10 * * *"` → fires daily at 10:00 UTC (3:30 PM IST).

---

## How the autonomous dispatch loop works

**Sweep body** (`run_coordinator_sweep_body` in `coordinator.py`):

Gate order (deterministic, zero-LLM):

1. **Global freeze** — `AGENT_AUTONOMY_GLOBAL_FREEZE` env → entire sweep muted, nothing dispatches.
2. **Per-tenant**: for every tenant with a `business_plan` row:
   - **3.5 CL-425 owner_inputs basis** — `_owner_inputs_enabled(tenant_id)` fail-closed. No dispatch
     and NO status write if absent.
   - **VT-384 stranded-batch re-arm** — re-arms demoted batches before gate 3.7 (one-open-per-tenant safe).
   - **3.7 approval serialization** — any open `pending_approvals` row defers the whole tenant until resolved.
   - **VT-384 autonomy offer** — best-effort L3 opt-in offer dispatch for eligible (tenant, agent).
   - **Per-registered agent** (only `sales_recovery` today):
     - `is_frozen(tenant_id, agent)` (PR-2 seam — always False in PR-1)
     - Open work item dedup (partial-unique INSERT … ON CONFLICT)
     - Dispatches **at most 1 item per tenant per sweep** via `DBOS.start_workflow(agent_dispatch_workflow, …)`

**Per-item dispatch workflow** (`agent_dispatch_workflow`):

1. Re-checks CL-425 owner_inputs (fail-closed, cancel the work item if revoked in the gap)
2. Opens a `pipeline_runs` row (uuid5 deterministic — exactly-once on DBOS recovery)
3. VT-374 run-control hold (`hold_while_paused_durable`) — paused dispatch survives restart
4. Calls `SalesRecoveryAgent.execute_item(ctx)` — the LLM lives HERE in the executor
5. Persists final work-item status

**The `SalesRecoveryAgent` executor** gates again at entry:
- `tenant_is_sr_eligible` (onboarding_gate.py) — `onboarding_journey.status='complete'` +
  `verification_status ∈ {gstin_verified, vtr_verified}` + ≥1 connected data source + ≥1 customer
- Fail-closed: a non-eligible tenant returns `skipped_not_onboarded` immediately

**Customer send path** (Gate 0 at `customer_send.agent_send_draft`):
- VT-421 onboarding gate re-checked
- Consent / opt-out / owner approval (L2 or L3) unchanged
- VT-476 dev send-guard at transport level — on dev, only `DEV_SEND_ALLOWLIST` numbers get real sends

**Nothing in the coordinator wiring relaxes any of these gates.** Activation = the dispatch loop runs;
sends stay fully compliance-gated.

---

## Caveat re: double-dispatch (cleared)

The supervisor's `run_sales_recovery_agent` (live inbound-message routing) and the coordinator's
sweep are DISTINCT triggers. The supervisor fires on owner-reply triggers during an active inbound
session. The coordinator fires on a SCHEDULED CRON for PROACTIVE action (dormant customer detection).
They do not share a dispatch path and cannot race on the same work item — the partial-unique INSERT
in `_claim_work_item` is the race-safe dedup for the coordinator path.

---

## Remaining gap: no HTTP kick endpoint

`kick_coordinator(tenant_id, …)` exists at `coordinator.py:299` (single-tenant manual trigger, same
gates) but is **NOT wired to any HTTP endpoint**. For the live run, Fazal must either:

1. Wait until 10:00 UTC (3:30 PM IST) for the scheduled DBOS cron to fire, OR
2. CC adds a thin ops endpoint (POST `/api/orchestrator/coordinator/kick`) so the run doesn't need
   to wait for the cron window.

**Recommend: CC adds the kick endpoint as part of the live-run prep** (low-risk, ops-only, no new
gate logic — just exposes the existing `kick_coordinator` function behind the ops auth surface).
This should be a VT row or an inline note on VT-431 before Fazal runs.

---

## Canary / validation (dev)

After the next dev deploy (which will include d709532):

1. Check Railway Dev logs for the DBOS registration log at boot — `register_agent_coordinator`
   should not raise.
2. At 10:00 UTC (or via kick endpoint once added), the `agent_coordinator_sweep` pipeline event
   should appear in the orchestrator logs with `tenants_scanned >= 0` and `dispatched = 0` (no
   eligible tenant yet — the Sundaram/RKeCom test tenant needs journey_complete + connector + customers
   to get dispatched).
3. Once the test tenant crosses all SR gates, the next sweep dispatches a work item and the
   `agent_draft_approval` template fires to Fazal's allowlisted number.

---

## Action items

| Action | Owner | Gate |
|---|---|---|
| Close VT-431 sprint row to `Done` | Cowork | After Cowork verifies d709532 diff |
| Add `/api/orchestrator/coordinator/kick` endpoint | CC | New VT row or addendum to VT-431 |
| Verify boot registration in Railway dev logs post-deploy | CC | After next dev deploy |
| Confirm sweep logs appear at 10 UTC | Fazal / CC | Live-run day |
