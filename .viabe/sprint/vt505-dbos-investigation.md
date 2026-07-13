# VT-505 DBOS Investigation — Root Cause & Fix

**Investigator:** Claude Code (read-only, 2026-06-30)
**Branch:** cc-winback-followups @ 35ee3b8
**Rule #14 standard:** all findings reconciled against Railway boot logs + live postgres_dbos_sys query + l3_hold.py source.

---

## HEADLINE: THE STATED PREMISE IS WRONG

The VT-505 ticket premise — "dbos.workflow_status absent, DBOS not initialized, pooler blocking init" — is **incorrect**. DBOS IS fully initialized on Railway dev. The real blocker is a **code bug in l3_hold.py** (infinite self-recursion in `_hold_demote_step`/`_hold_send_step` wrapper functions), not an infrastructure failure.

---

## Evidence Base

### 1. DBOS IS initialized and the system DB exists

**Railway dev boot log (latest deployment, vt-orchestrator-service):**
```
11:47:14 [INFO] (dbos:_sys_db.py:511) Initializing DBOS system database with URL:
    postgresql://postgres.ibtsitmevhzirrhwuryu:***@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres_dbos_sys (schema: dbos)
11:47:14 [INFO] (dbos:_app_db.py:73) Initializing DBOS application database with URL:
    postgresql://postgres.ibtsitmevhzirrhwuryu:***@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres (schema: dbos)
11:47:21 [WARNING] (dbos:_dbos.py:586) Current version 'd535d58410370cd660cb824a05c82450' is not
    the latest version. Latest version is '36aceb4332ac85915b7a8f26b0498415'.
11:47:21 [INFO] (dbos:_dbos.py:709) DBOS launched!
```

No error. No exception. DBOS launched cleanly on every deployment.

**Queried dev Supabase pg_database:**
```
Databases: ['postgres', 'postgres_dbos_sys']
```
`postgres_dbos_sys` exists. DBOS created it successfully on first boot.

**Queried dbos schema in postgres_dbos_sys:**
```
dbos schema tables in postgres_dbos_sys:
  application_versions, dbos_migrations, event_dispatch_kv,
  notifications, operation_outputs, queues, streams,
  workflow_events, workflow_events_history, workflow_schedules,
  workflow_status   ← PRESENT
```

### 2. Why `transaction_outputs` is in `postgres` but `workflow_status` is not

DBOS uses TWO databases:
- **System DB** (`postgres_dbos_sys` via pooler): holds `workflow_status`, `operation_outputs`, `workflow_schedules`, `queues`, `notifications` — all DBOS durable-execution state. DBOS derives this URL automatically: `database_url.database + "_dbos_sys"` (see `dbos._dbos_config.get_system_database_url`).
- **Application DB** (`postgres` via pooler): holds `transaction_outputs` (the `ApplicationSchema` table in `application_database.py`).

The investigation queried only `DATABASE_URL` → `postgres`. `workflow_status` is not in `postgres` — it was never supposed to be. The initial premise mistook `transaction_outputs` (app DB) for evidence of partial init.

### 3. Scheduled crons ARE firing

Queried `dbos.workflow_status` in `postgres_dbos_sys`:
```
Workflow status counts:
  SUCCESS: 91
  PENDING: 1    ← test-purge
  ERROR:   1    ← l3_hold recursion bug (see below)
  ENQUEUED: 1   ← test-purge
  DELAYED: 1    ← test-purge

All workflow names:
  alerts_sweep_body:                29 runs, 29 ok, 0 err
  ingestion_scheduler_body:         28 runs, 28 ok, 0 err
  poll_unwatched_sheets_body:       14 runs, 14 ok, 0 err
  l2_approved_send_sweep_scheduled: 10 runs, 10 ok, 0 err
  approval_timeout_sweep_scheduled:  5 runs,  5 ok, 0 err
  purge_workflow_inputs_scheduled:   4 runs,  4 ok, 0 err
  sla_breach_sweep_scheduled:        2 runs,  2 ok, 0 err
  agent_coordinator_scheduled:       1 run,   1 ok, 0 err
  agent_dispatch_workflow:           1 run,   1 ok, 0 err
  l3_hold_workflow:                  1 run,   0 ok, 1 err  ← RECURSION BUG
```

The VT-431 autonomous coordinator (`agent_coordinator_scheduled`), all alert sweeps, ingestion scheduler, L2 reconciler sweep — all running correctly.

### 4. DATABASE_URL and connection type

Railway dev env:
- `DATABASE_URL`: set → confirmed equal to the session-mode pooler URL `aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres`
- `TEAM_SUPABASE_DB_URL`: unset (so DBOS reads `DATABASE_URL`)
- `DBOS_SYSTEM_DATABASE_URL`: unset (DBOS derives `postgres_dbos_sys` automatically)

Connection type: **Supabase session-mode pooler** (port 5432 via pooler host, project-ref username). This is NOT the transaction-mode pooler (port 6543). Session mode supports advisory locks, prepared statements, and schema migrations. DBOS's CREATE DATABASE succeeded on this connection (Supabase postgres user has CREATEDB).

The pooler concern was valid as a hypothesis but does not apply here — DBOS connected and ran migrations on first boot without issue.

### 5. Version mismatch (non-blocking)

Current running version `d535d5...` is not the latest `36aceb...` in the system DB. This is expected after a code change (DBOS computes a hash of all workflow/step source code). DBOS recovery only recovers workflows from the current version — a non-blocking warning. New workflows can still be created.

---

## THE REAL BUG: RecursionError in `l3_hold_workflow`

### Evidence

Workflow `l3_hold_63477e2c-9f7f-4dc4-80a2-f64b56847ea7` in `postgres_dbos_sys`:
```
status: ERROR
name: l3_hold_workflow
application_version: d535d5841037...   ← current branch
error: RecursionError: maximum recursion depth exceeded
```

This is the ACTUAL launch blocker for L3. It fires when `l3_hold_workflow` reaches a `"demote"` or `"send_now"` decision.

### Root Cause: Name-shadowing + self-recursion in l3_hold.py

**File:** `apps/team-orchestrator/src/orchestrator/agents/l3_hold.py`

**Lines 717–741:**
```python
_hold_demote_step: Any | None = None          # line 717 — sets global to None
_hold_send_step: Any | None = None            # line 718

def _ensure_hold_steps() -> None:
    global _hold_demote_step, _hold_send_step
    if _hold_demote_step is None:             # ALWAYS FALSE after line 732 defines the function
        _hold_demote_step = DBOS.step()(_hold_demote_step_body)
    if _hold_send_step is None:               # ALWAYS FALSE after line 738 defines the function
        _hold_send_step = DBOS.step()(_hold_send_step_body)

def _hold_demote_step(tenant_id: str, batch_id: str) -> None:   # line 732 — REBINDS global to function
    _ensure_hold_steps()       # no-op: _hold_demote_step is not None (it's THIS function)
    assert _hold_demote_step is not None       # True — it's the function
    return _hold_demote_step(tenant_id, batch_id)  # CALLS ITSELF → infinite recursion

def _hold_send_step(tenant_id: str, batch_id: str) -> dict[str, Any]:  # line 738 — same bug
    _ensure_hold_steps()
    assert _hold_send_step is not None
    return _hold_send_step(tenant_id, batch_id)    # CALLS ITSELF → infinite recursion
```

**The mechanism:**
1. At module load time: `_hold_demote_step: Any | None = None` sets the module-level name to `None`.
2. Then `def _hold_demote_step(...)` **rebinds** the same module-level name to the function object.
3. `_ensure_hold_steps()` checks `if _hold_demote_step is None:` — it is NOT None (it's the function). The DBOS wrapper (`DBOS.step()(_hold_demote_step_body)`) is never assigned.
4. When `l3_hold_workflow` calls `_hold_demote_step(tenant_id, batch_id)` (line 650), it calls the function at line 732, which calls itself → `RecursionError`.

**Contrast with l2_send.py (correct pattern):**
```python
_l2_send_step_decorated: Any | None = None   # global with DISTINCT name

def _l2_send_step(tenant_id, batch_id):      # wrapper with DIFFERENT name from global
    _ensure_l2_send_step()                   # sets _l2_send_step_decorated if None
    return _l2_send_step_decorated(...)       # calls the DECORATED version, not itself
```

l2_send.py uses `_l2_send_step_decorated` (distinct name) so the wrapper function `_l2_send_step` does NOT shadow the global and no recursion occurs.

**l3_hold.py uses the WRONG pattern:** the wrapper function name matches the global name it's supposed to guard, causing the global to never be the DBOS wrapper.

---

## IMPACT ASSESSMENT

| Path | Status | Reason |
|---|---|---|
| Scheduled crons (alerts, ingestion, coordinator) | **Working** | All show SUCCESS in workflow_status |
| L2 owner-approved send (`start_l2_send`) | **Working** (untested on this dev instance) | `l2_send_workflow` uses correct pattern; no recursion bug; l2 sweep shows 10 SUCCESS runs |
| L3 autonomous hold (`start_l3_hold` / "wait" loop) | **Partially working** | The workflow starts and polls ("wait" path); but hits RecursionError on "demote" or "send_now" transition |
| L3 send-now delivery | **BLOCKED** | RecursionError in `_hold_send_step` |
| L3 no-delivery demote | **BLOCKED** | RecursionError in `_hold_demote_step` |
| VT-431 autonomous coordinator | **Working** | 1 run, SUCCESS |

The DBOS infrastructure (init, pooler, schema) is NOT the blocker. The L3 hold final-mile (demote + send-now transitions) is the blocker due to a code bug.

---

## PROPOSED FIX

**Type: CODE fix only. No infra change, no Railway env var change, no DB change needed.**

**File:** `apps/team-orchestrator/src/orchestrator/agents/l3_hold.py`

**Option A — Match the l2_send.py pattern (rename decorated globals to `_..._decorated`):**

```python
# Line 717-718: rename the globals
_hold_demote_step_decorated: Any | None = None
_hold_send_step_decorated: Any | None = None

# Line 721-729: update _ensure_hold_steps to set the renamed globals
def _ensure_hold_steps() -> None:
    from dbos import DBOS
    global _hold_demote_step_decorated, _hold_send_step_decorated
    if _hold_demote_step_decorated is None:
        _hold_demote_step_decorated = DBOS.step()(_hold_demote_step_body)
    if _hold_send_step_decorated is None:
        _hold_send_step_decorated = DBOS.step()(_hold_send_step_body)

# Line 732-741: wrappers now call the DECORATED version (no longer self-recursive)
def _hold_demote_step(tenant_id: str, batch_id: str) -> None:
    _ensure_hold_steps()
    assert _hold_demote_step_decorated is not None
    return _hold_demote_step_decorated(tenant_id, batch_id)

def _hold_send_step(tenant_id: str, batch_id: str) -> dict[str, Any]:
    _ensure_hold_steps()
    assert _hold_send_step_decorated is not None
    return _hold_send_step_decorated(tenant_id, batch_id)
```

**Option B — Inline init in `l3_hold_workflow` (same pattern as `_hold_state_step`):**

Remove the `def _hold_demote_step(...)` and `def _hold_send_step(...)` wrapper functions entirely.
In `l3_hold_workflow`, initialize all three steps in the same block:
```python
global _hold_state_step, _hold_demote_step, _hold_send_step
if _hold_state_step is None:
    _hold_state_step = DBOS.step()(_hold_state_body)
if _hold_demote_step is None:
    _hold_demote_step = DBOS.step()(_hold_demote_step_body)
if _hold_send_step is None:
    _hold_send_step = DBOS.step()(_hold_send_step_body)
```

This requires removing `_ensure_hold_steps()` and changing lines 650/653 to call `_hold_demote_step(...)` and `_hold_send_step(...)` directly (which would now be the DBOS wrappers, not the plain functions).

Option A is cleaner — it matches the existing l2_send.py idiom and preserves the `_ensure_*` lazy-init structure.

---

## WHAT DBOS_SYSTEM_DATABASE_URL WOULD CHANGE

If someone sets `DBOS_SYSTEM_DATABASE_URL` (as the VT-505 ticket discussed), DBOS would use that value instead of auto-deriving `postgres_dbos_sys`. This would be needed if DBOS could NOT create `postgres_dbos_sys` on Supabase. It is NOT needed here — `postgres_dbos_sys` already exists and works. Setting `DBOS_SYSTEM_DATABASE_URL` to the same pooler URL with `/postgres_dbos_sys` would be equivalent to what's already happening.

---

## SUMMARY

| | Stated Premise | Actual |
|---|---|---|
| `workflow_status` absent | Yes (in `postgres`) | In `postgres_dbos_sys` — CORRECT location |
| DBOS not initialized | Yes | DBOS launches cleanly on every deploy |
| Pooler blocks DBOS init | Possible | Session-mode pooler works; DBOS created system DB |
| Crons not firing | Yes | 91 SUCCESS runs; all crons confirmed firing |
| L2 owner-approved send blocked | Yes | No recursion bug in l2_send; should work when triggered |
| L3 auto-send blocked | Yes | **RecursionError in `_hold_demote_step`/`_hold_send_step` — confirmed via ERROR workflow in postgres_dbos_sys** |

**Fix type: Code (l3_hold.py). Severity: L3 terminal paths (demote + send-now). L2 + DBOS infra: not blocked.**
