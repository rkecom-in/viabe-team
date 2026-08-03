# VT-634 — Containment design for failed / orphaned DBOS workflows in PROD

**Author:** Claude Code (Lane C, design-only, 2026-08-03)
**Status:** DESIGN. No runtime code changed. Nothing run against prod. No VT-IDs or migration numbers allocated.
**Row:** `.viabe/sprint/VT-634.md` · **Spec:** `.viabe/prod-failed-workflow-handling-spec.md` (authoritative; this document makes it mechanical)
**Verified against:** DBOS **2.22.0** source at `apps/team-orchestrator/.venv/lib/python3.13/site-packages/dbos/` — every DBOS claim below carries a `file:line`.

---

## The failure this prevents

A production workflow crashes mid-flight. The orchestrator restarts. DBOS's startup recovery re-invokes it from
its checkpointed inputs. It crashes again. Restart, re-invoke, crash — up to **100 times** per workflow
(`_registrations.py:10`), each attempt a full re-execution of real business logic against real tenants. Two
distinct harms fall out:

1. **Resource + correctness churn.** This is the dev incident, live: a `manager_review` task crash-looping on
   `psycopg.errors.ForeignKeyViolation: incidents_tenant_id_fkey` because its tenant had been torn down. 14 PENDING
   + 4 ENQUEUED rows are sitting in dev's `dbos.workflow_status` right now, oldest from **2026-05-21**, and nothing
   in the system will ever clear them (§0.6).
2. **Duplicate customer sends.** If the crashed workflow had already dispatched some customer messages, every
   recovery attempt is another chance to re-send them. The only thing standing between a crash-loop and a
   double-send today is the *application-level* per-unit ledger (§3) — not anything DBOS provides.

And the mirror-image failure, equally unacceptable: silently disabling the workflow so a half-sent campaign is
simply dropped, with some customers messaged, others not, and the owner believing it went out.

**Not the timeout story.** The 90s scenario TIMEOUTs observed 2026-08-03 were root-caused separately to VT-729
(Railway native auto-deploy restarts the orchestrator on *every* push to dev, including docs-only pushes the CI
trigger-diet skips). This design does not claim orphans caused those timeouts. The connection is narrower and
one-directional: **VT-729's restart cadence is what sets how often an uncontained orphan re-fires** (§0.4).

---

## §0 — Ground truth: what DBOS 2.22.0 actually does

Seven findings, each of which changed the design. Items 4–7 contradict assumptions that were in play going in
(including one written into a live docstring in our own code).

### 0.1 Recovery is unconditional and unfilterable
`DBOS.launch()` reads every PENDING row matching `(executor_id, application_version)` and submits them to a
recovery thread (`_dbos.py:604-618` → `_recovery.py:52-74`). There is **no hook, callback, or config knob** to
filter what gets recovered. The only switches are `conductor_key` (disables local recovery entirely, `_dbos.py:605`)
and DBOS Cloud. ⇒ Containment cannot be a policy *inside* DBOS. It must change the row's status *before* launch.

### 0.2 `cancel` is the containment primitive, and it is out-of-process capable
`SystemDatabase.cancel_workflows` (`_sys_db.py:764-789`) sets status → `CANCELLED` and clears `queue_name`, for
any workflow not already SUCCESS/ERROR. `get_pending_workflows` (`_sys_db.py:1649-1671`) selects `status = PENDING`
only, and the queue dequeue path is likewise app_version-filtered (`_queue.py:483-506`). ⇒ **A CANCELLED workflow is
invisible to both recovery and the queue.** That is exactly the containment semantic the spec asks for.

Better still, `DBOSClient` (`_client.py:145-192`) opens the system DB **without running migrations, without
registering workflows, and without launching an app** ("We only create database connections but do not run
migrations", `_client.py:174`; `pool_size=2, max_overflow=0`). It exposes `list_workflows`, `list_workflow_steps`,
`cancel_workflow(s)`, `resume_workflow(s)`, `fork_workflow`, `delete_workflow`. ⇒ The sweep and the diagnosis
reader can be an ordinary process that never triggers recovery as a side effect of inspecting.

### 0.3 `cancel` is cooperative, not a kill
The CANCELLED check fires when a step looks up its checkpoint (`_sys_db.py:2026-2030`, raising
`DBOSWorkflowCancelledError`); the async preemptible-step poller (`_core.py:1551-1570`) is opt-in and not used on
our paths. ⇒ Cancel guarantees **no further steps and no future recovery**. It does *not* guarantee that a step
already inside a Twilio call stops. Containment is a *stop-advancing* primitive, and the design must not pretend
otherwise (§3 handles the in-flight effect).

### 0.4 CORRECTION — `application_version` is a source hash, not a deploy id
`compute_app_version` is an MD5 over `inspect.getsource()` of every registered workflow, sorted, plus the DBOS
version string (`_dbos.py:274-296`). Our own `orphan_reaper.py:8-11` states *"a redeploy changes the app_version, so
a run stranded by the previous deploy is never re-invoked."* **That is wrong in general.** A restart that does not
change any workflow function body reproduces the identical hash and re-recovers the same PENDING rows. Per VT-729,
Railway restarts the orchestrator on *every* push to dev — including docs-only pushes, which by construction change
no workflow source. That is the mechanism by which a 2026-05-21 PENDING row is still being picked up in August.

The reaper's *behaviour* is still correct (its 1-hour age floor is safe either way); only its stated reasoning is
wrong. Worth fixing that docstring when this row is built, because the wrong model is what makes orphans look
self-limiting when they are not.

### 0.5 CORRECTION — `max_recovery_attempts` is a backstop, not containment
`DEFAULT_MAX_RECOVERY_ATTEMPTS = 100` (`_registrations.py:10`). The counter increments on every execution attempt
and flips the row to `MAX_RECOVERY_ATTEMPTS_EXCEEDED` past the budget (`_sys_db.py:698-727`). ⇒ It fires *after* 100
full re-executions. For an effectful workflow that is 100 chances to re-send. Useless as the containment mechanism —
but a cheap defence-in-depth: `DBOS.workflow(max_recovery_attempts=N)` is available (`_dbos.py:976`), and because
app_version hashes the *workflow body* and not the `register_*()` call site, tightening it on the effectful
workflows does **not** perturb app_version. Recommend `max_recovery_attempts=3` on `l2_send_workflow`,
`l3_hold_workflow`, `manager_task_workflow`, `agent_dispatch_workflow` as a belt-and-braces change alongside the
real fix.

### 0.6 CORRECTION — GC will never clear orphans, and cancelling makes evidence GC-eligible immediately
`garbage_collect` deliberately excludes PENDING / ENQUEUED / DELAYED (`dbos_purge.py:114-118`). ⇒ Orphans accumulate
**forever**; that is why 2026-05-21 is still on the board. The corollary is the sharp edge: the instant containment
flips a row to CANCELLED it becomes GC-eligible, and `dbos_purge` runs **every 30 min with a 2h retention cutoff**
(`dbos_purge.py:61-67`), cascading to `operation_outputs` / the step ledger. An orphan from May is decades past the
cutoff. ⇒ **Snapshot before cancel, in that order, or the diagnosis evidence is deleted within 30 minutes of
containing it.** This ordering is load-bearing and is the single easiest thing to get wrong when building this.

### 0.7 CORRECTION — containment cannot live with the existing reapers
`main.py:191-209` runs `reap_orphan_runs` / `reap_stalled_manager_tasks` / `detect_silent_terminal_runs` **after**
`launch_dbos()`, deliberately ("so DBOS's own same-version recovery has already fired"). For those reapers that is
right — they heal application rows recovery left behind. For containment it is fatal: by then `DBOS.launch()` has
already submitted the orphans to the recovery thread. ⇒ The quarantine sweep is a **pre-launch** phase, structurally
separate from the reaper block.

### 0.8 CORRECTION — the quarantine record cannot be an `incidents` row
`incidents.tenant_id UUID NOT NULL REFERENCES tenants(id)` (`migrations/156_vt552_incidents.sql:20`) — this is
`incidents_tenant_id_fkey`, the exact constraint in the dev crash-loop. The *dominant* orphan class is "tenant is
gone", and that class **cannot be written to `incidents` at all**. A containment record that fails to insert for the
most common case is not a containment record. The quarantine store must be tenant-*nullable* and FK-free.

---

## §1 — Disposition: the classes and the rule that assigns one

The premise the spec is built on: *"a stuck PENDING whose parent row is gone is not the same as one whose dependency
is transiently down."* Making that mechanical needs a discriminator that is not "PENDING and old".

**"PENDING and old" is not a valid orphan test.** `l3_hold_workflow` (`agents/l3_hold.py:620-650`) is a durable poll
loop — `_hold_state_step` / `DBOS.sleep(_HOLD_POLL_S)` — that legitimately sits PENDING for the whole hold window.
`manager_task_workflow` parks on owner approval the same way. Age alone cannot tell a healthy park from a zombie.

**The discriminator is the domain anchor.** Every workflow id in this codebase is deterministic and *encodes its
domain key*:

| Workflow | ID format | Anchor row |
|---|---|---|
| `l2_send_workflow` | `l2_send_{batch_id}` (`agents/l2_send.py:208`) | `agent_draft_batches` |
| `l3_hold_workflow` | `l3_hold_{batch_id}` (`agents/l3_hold.py:774-777`) | `agent_draft_batches` |
| `manager_task_workflow` | `manager_task:{tenant_id}:{task_id}` (`manager/workflow.py:1427-1432`) | `manager_tasks` |
| `webhook_pipeline_run` | `{run_id}` (`runner.py:624`) | `pipeline_runs` |
| WhatsApp signup / customer inbound | `wa_signup_{sid}` / `wa_customer_{sid}` (`api/twilio_ingress.py:293`, `integrations/customer_inbound.py:216`) | `pipeline_runs` / message row |

So the classifier resolves the anchor **from the workflow id**, never from `workflow_status.inputs`. That is not
just convenient — `inputs` carries the raw Twilio `Body` verbatim (`dbos_purge.py:3-12`) and is purged on a 2h
clock, so it is both a PII hazard and unreliable. The sweep must call `list_workflows(..., load_input=False)`.

### The classes

| # | Class | Deterministic test | Disposition |
|---|---|---|---|
| **G** | **Legitimately parked** | Anchor exists AND is in the state that workflow is *designed* to wait on (batch `auto_send_pending` for `l3_hold`; task `waiting_owner` for `manager_task`) AND age is within that workflow's designed window | **Not a candidate.** Leave alone. Never contained. |
| **A** | **Anchor gone** | Anchor id resolves to no row (tenant deleted → cascade, or the row itself deleted) | **Contain, terminal.** Never resumable — there is nothing to resume *for*. |
| **B** | **Anchor closed** | Anchor row exists but is terminal / no longer wants the work (batch `cancelled` / `sent`, task `dead_letter`, run `aborted_hard_limit`) | **Contain, terminal.** The work is not wanted. |
| **C** | **Anchor open, effects clean** | Anchor wants the work; **no** effect-ledger row shows a dispatch for any unit | **Contain, HOLD.** An operator may authorise a scoped remainder run (§3). No auto-resume. |
| **D** | **Anchor open, effects PARTIAL** | ≥1 unit ledgered `sent` **and** ≥1 unit still pending | **Contain, HOLD.** The hard case — §3. Owner-visible consequence is a Fazal decision (§6.1). |
| **E** | **Effect indeterminate** | ≥1 unresolved `sending` marker (the VT-420 crash window) | **Contain, HOLD, human-only.** Nothing automatic ever touches this class. |
| **F** | **Transient dependency** | Anchor open, effects clean, and the recorded error signature matches a known-transient class (connection reset, pooler exhaustion, 5xx from a vendor) | **Contain** the DBOS workflow; hand the retry to the **existing application ladder** (`task_retry.decide_retry` / `reap_stalled_manager_tasks`), never to DBOS recovery. |

### Two properties the classifier must have

**Total.** An unrecognised workflow-name or id shape must land in a **fail-safe class (E, human-only)** — never "no
anchor found, therefore class A, therefore discard". Class A requires *positively resolving* the anchor kind and
*then* finding the row absent. This follows the fail-loud posture: an unknown workflow is an unregistered effect
surface, and unregistered means unknown blast radius.

**Registry-backed.** Class assignment for C/D/E depends on knowing where a workflow's per-unit effect ledger lives.
That needs an explicit registry: `workflow_name → (anchor_kind, unit_query, idempotency_key_format)`. A workflow
absent from that registry is class E by construction. This registry is also the enforcement point for the invariant
in §3.4.

**No LLM anywhere in this path.** Classification, containment and effect-state reads are deterministic SQL. The
console renders facts; a human decides.

---

## §2 — Containment: stop it without re-running it, keep it fully inspectable

### Mechanism

**Phase 1 — pre-launch quarantine sweep.** In `main.py` lifespan, **before** `launch_dbos()` (§0.7):

```
DBOSClient(system_database_url=…)                      # no migrations, no registration, no recovery (§0.2)
  → list_workflows(status=[PENDING, ENQUEUED, DELAYED], load_input=False)
  → for each: classify (§1)
  → for each non-G, non-F:
        1. SNAPSHOT  — write the quarantine row, including list_workflow_steps() summary
        2. CANCEL    — client.cancel_workflow(workflow_id)
  → destroy()
then DBOS(config); DBOS.launch()                        # finds nothing PENDING to recover for those ids
```

The two-step order in that loop is the §0.6 finding: cancel-then-snapshot loses the evidence to the 30-minute purge.
Snapshot must commit first.

Fail-soft, like every other boot sweep in this codebase (`orphan_reaper.py` idiom): a sweep failure logs loudly and
**never blocks boot** — with one deliberate asymmetry stated in §5.

**Phase 2 — steady-state re-sweep.** Not every orphan is created by a restart. A workflow can strand while the
process lives (an ERROR row nobody handles, a `sending` marker that goes stale). A scheduled re-sweep runs hourly
alongside `orphan_run_reaper_scheduled`, doing classification + snapshot + cancel for anything newly eligible, plus
staleness detection for unresolved `sending` markers (which today produce only a log line — see §3.3).

### Why not the alternatives

- *Let `max_recovery_attempts` handle it* — fires after 100 re-executions (§0.5). Damage first, containment after.
- *Delete the workflow row* — `delete_workflow` exists (`_client.py:549`) and is the wrong answer: it destroys the
  step ledger, which is the diagnosis input. GC on the retention clock is the right eventual disposal, once the
  snapshot is durable.
- *Set `conductor_key` to move recovery off-box* — disables local recovery (`_dbos.py:605`) but replaces our policy
  with DBOS's, which we do not control. See §5 for what must be checked here regardless.

### The quarantine store

A new table (shape below; **migration number allocated at build time via `scripts/migration_id_allocate.py`, per
CL-424 — not allocated here**). Explicitly **not** `incidents`, per §0.8.

```
workflow_quarantine
  workflow_id            TEXT PRIMARY KEY        -- the DBOS id; also the anchor join key
  workflow_name          TEXT NOT NULL
  tenant_id              UUID NULL               -- NO FK. Class A has no tenant. This is the §0.8 fix.
  anchor_kind            TEXT NULL               -- 'batch' | 'task' | 'run' | NULL when unresolvable
  anchor_id              TEXT NULL
  dbos_status_at_capture TEXT NOT NULL           -- PENDING | ENQUEUED | DELAYED | ERROR
  recovery_attempts      INT  NOT NULL           -- how many times it had already re-run
  class                  TEXT NOT NULL           -- A..F
  error_signature        TEXT NULL               -- REDACTED exception class + constraint name, never a message body
  effect_state           JSONB NOT NULL          -- {sent: n, pending: n, indeterminate: n, units: [...]}
  steps_summary          JSONB NOT NULL          -- from list_workflow_steps: names + function_ids + error flags only
  first_seen_at          TIMESTAMPTZ NOT NULL
  contained_at           TIMESTAMPTZ NOT NULL
  disposition            TEXT NOT NULL           -- 'held' | 'closed_no_action' | 'remainder_run' | 'escalated'
  resolution             JSONB NULL              -- who, when, typed reason, remainder batch id
  resolved_by            TEXT NULL
  resolved_at            TIMESTAMPTZ NULL
```

RLS: ops-visible via the `operator_claim` predicate (the `incidents_operator_select` pattern,
`migrations/156_vt552_incidents.sql:54-61`), **not** tenant-scoped — a `tenant_id IS NULL` row must still be
readable, which a `tenant_id = app_current_tenant()` policy would hide. Redaction discipline is CL-390: ids,
statuses, counters, exception *classes*. No message bodies, no phones, no draft params.

### What "fully inspectable" means concretely

After containment, for any quarantined workflow an operator can see: which workflow and which tenant (or that the
tenant is gone), which anchor row, what state that anchor is in, how many times it had already re-run, which steps
completed and which raised, the redacted failure signature, and — the part that matters — the per-unit effect state.
None of that requires the DBOS row to still exist, which is what lets GC eventually reclaim it.

---

## §3 — The partial-send problem

### 3.1 The substrate already exists — read it before proposing anything

VT-44/VT-45/VT-420/VT-423 built most of the answer:

- **`send_idempotency_keys`** (`migrations/049_outbound_send_ledger.sql`) — one row per send attempt, unique on
  `(tenant_id, idempotency_key)`. For the agent send path the key is `agent:{draft_id}`
  (`agents/customer_send.py:682`). This row **is** the per-unit effect record.
- **VT-420 in-flight marker** — a `'sending'` row is written **and committed before** the Twilio `messages.create`
  call and flipped to `'sent'` after (`agent/tools/send_whatsapp_template.py:193-202, 355-378`). The pool is
  autocommit, so the marker is durable the instant it executes.
- **VT-423 permanent block** — a `'sending'` marker is explicitly **not** time-bounded in the idempotency query
  (`send_whatsapp_template.py:265-278`): terminal statuses expire on a 24h TTL, `sending` never does. A stale marker
  blocks re-dispatch *forever*, by design, "until it resolves to a terminal state or a separate reconciler sweeps a
  genuinely-stuck marker" — with a loud log as the hand-off (`_warn_if_stale_marker`, lines 212-234).
- **VT-387 exclusion** — `'error'` is deliberately *not* an idempotent hit, and the invariant that makes that safe is
  documented at `send_whatsapp_template.py:178-188`: `'error'` is never written after a successful side effect.

**That reconciler does not exist.** VT-423 named the hand-off and left it unbuilt. VT-634 is its owner.

### 3.2 How partial completion is determined

The unit of partial completion is the **draft**, not the workflow. For a quarantined `l2_send_{batch_id}` /
`l3_hold_{batch_id}`, take `batch_id` from the workflow id, enumerate the batch's drafts, and classify each unit by a
three-layer read in this precedence:

| Layer | Source | Verdict |
|---|---|---|
| 1 | `send_idempotency_keys` for `agent:{draft_id}` | `sent` (+`message_sid`) → **SENT** · `sending` → **INDETERMINATE** · `error`/`window_closed`/`rate_limited` → **PENDING** · no row → **PENDING** |
| 2 | `agent_drafts.status` | Cross-check. `sent` with no ledger row, or `drafted` with a ledger `sent`, is a **contradiction** → force INDETERMINATE, flag for review |
| 3 | `agent_customer_contacts.delivery_status` (Twilio status callback, `customer_send.py:985-1040`) | The only evidence of actual *delivery*. A `message_sid` means Twilio accepted, not that it arrived — `customer_send.py:957` says this explicitly |

Effect-state is then the triple `(sent, pending, indeterminate)`. Class D is `sent ≥ 1 ∧ pending ≥ 1`. Class E is
`indeterminate ≥ 1`, and E dominates every other class — a single unresolved marker makes the whole workflow
human-only.

### 3.3 The rules

**Never blindly re-run.** No path in this design calls `resume_workflow` or `fork_workflow` on a workflow that
carried customer-visible effects. Not as a button, not as a policy, not in prod, not in dev.

It is worth being precise about *why*, because the obvious reason is wrong. DBOS resume is **not** a blind re-run:
completed `@DBOS.step`s replay their memoised output from `operation_outputs` rather than re-executing. But that
protection is **per-step**, and `_l2_send_step_body` (`agents/l2_send.py:82-138`) is **one step wrapping a loop over
N drafts**. A crash at draft 7 of 20 records no step output, so recovery re-runs the entire loop and drafts 1–6
re-enter `agent_send_draft`. They survive because of the *application* ledger (selection filters `status='drafted'`;
the `agent:{draft_id}` hit short-circuits inside `send_whatsapp_template`) — **not** because of DBOS.

⇒ **DBOS step granularity provides no partial-send protection. Only the per-unit ledger does.** Which yields the
invariant in §3.4.

**Never silently drop.** Every contained workflow with `sent ≥ 1` or `pending ≥ 1` produces a durable
`workflow_quarantine` row and an alert. Class D additionally requires an owner-facing consequence — the *content* of
which is Fazal's call (§6.1), and is the one place this design deliberately stops.

**Never auto-resolve an indeterminate unit.** Flipping a `sending` marker to `error` unblocks a re-send of a message
that may already have reached a customer. Only a human, having checked the Twilio console for that SID, may resolve
it — and the resolution records the evidence. There is **no timer** that expires a `sending` marker. VT-423 already
made this call; nothing here may undo it.

**Complete the remainder, never the original.** The spec's step 3 ("inject a new PARTIAL sub-process") becomes: the
quarantined workflow stays CANCELLED forever; an operator action creates a **new** batch containing *only* the units
in state PENDING, and starts a fresh workflow over it. Units that are SENT or INDETERMINATE are excluded by
construction. This falls out cleanly from the id scheme — since the workflow id is `l2_send_{batch_id}`, a new batch
id yields a new workflow id that cannot collide with the quarantined one, and the existing exactly-once start plus
per-draft ledger dedup apply to the remainder run unchanged.

### 3.4 The invariant this design depends on

> **No workflow may perform a customer-visible effect that is not recorded in a per-unit idempotency ledger,
> written and committed BEFORE the effect.**

`send_whatsapp_template` satisfies this today (VT-420). The registry in §1 is where it gets enforced: a workflow
that is not registered with a `unit_query` + `idempotency_key_format` is classified **E** — human-only — because we
cannot determine what it did. That makes the invariant fail-loud rather than fail-silent, and it means a future
effectful workflow that skips the ledger degrades to "a human must look at it" instead of "we assumed it was clean".

---

## §4 — What the VTR / Ops console surfaces

### Where it goes, and why not run-control

`/team/ops/run-control` is structurally the wrong home. It is built as tenant tiles → program tiles → run timeline
(`apps/team-web/app/(app)/team/ops/run-control/page.tsx:1-40`), scoped by `scopeTenantsForOperator`. **Class A has no
tenant to scope to** and would be invisible there — and class A is the dominant class. The quarantine queue must be
a **cross-tenant, VTAdmin-tier** surface: a new `/team/ops/workflows` page (or a Quarantine section on
`/team/ops/monitoring`), gated by `requireOpsOperator` at the exception tier the way cross-tenant assignment already
is (`api/ops_vtr_console.py:163`).

### Minimum actionable row

Nine fields, because fewer means the operator cannot decide and more means they will not read it:

1. Workflow name + id
2. Tenant — business name, or a hard **`TENANT DELETED`** marker for class A
3. Class (A–F) with the one-line reason the classifier assigned it
4. **Effect state as three counters: `sent / pending / indeterminate`** — the field the whole design exists to
   produce, and the first thing the eye should land on
5. `contained_at` + `recovery_attempts` at capture (how many times it re-ran before we stopped it)
6. Redacted error signature (exception class + constraint name; never a message body)
7. Steps: completed / failed, with the failing step name
8. Anchor state — what the batch/task/run says now
9. Disposition + who resolved it + the typed reason

### Who decides, and what they can do

| Action | Available on | Gate |
|---|---|---|
| **Complete the remainder** — build the PENDING-only batch and start a fresh workflow | C, D — **disabled whenever `indeterminate ≥ 1`** | Typed reason mandatory. Authorisation tier is **§6.2** (open) |
| **Resolve an indeterminate unit** — flip one `sending` marker to `sent`/`error` after checking the SID | E | Per-unit. Operator must record the delivery evidence. The **only** thing that unblocks the VT-423 permanent block |
| **Close as no-action** | A, B, and C/D once the operator judges it settled | Typed reason mandatory; terminal |
| **Escalate** — open an `escalations` row (mig 073, the VTR queue) | Any | Standard ladder |

Every action writes a fail-closed `tm_audit` row before the mutation, in the same transaction — the
`vtr_ownership_decision` pattern (`api/ops_vtr_console.py:552-558`): can't-audit ⇒ can't-decide.

### Deliberately NOT buttons

- **No "Retry" / "Resume".** Nothing in this console calls `resume_workflow`. Resuming re-enters arbitrary code with
  effect boundaries we have not proven. The only forward path is a scoped remainder run.
- **No "Delete workflow".** Destroys the step ledger. GC handles disposal on the retention clock, after the snapshot.
- **No bulk action on classes D or E.** One at a time, one typed reason each. Bulk is available for A and B only,
  where by definition nothing was sent and nothing is wanted.
- **No auto-resolve timer for indeterminate units.** Not a button, not a cron, not a config knob.
- **No owner-facing message composed by the console.** Whatever the owner is told about a half-sent campaign is
  §6.1, and until that is decided the console escalates rather than improvises.

---

## §5 — Prod vs dev, and what must never be automated in prod

### Behavioural differences

| | Dev | Prod |
|---|---|---|
| Class A frequency | Dominant + expected — the convo harness tears tenants down constantly | Rare and **significant**: a real tenant was deleted with work in flight (churn or a DPDP erasure) |
| Class A handling | Auto-contain, log only, **no alert** (otherwise it is pure noise, the VT-620 test-tenant-reaper lesson) | Auto-contain **+ alert**, always |
| Sweep failure | Log and continue | See "the one asymmetry" below |
| Remainder runs | May be exercised end-to-end against bogus fixtures | Fazal-gated (§6.2); a real customer send |

**The one asymmetry.** Every boot sweep in this repo is fail-soft so a sweep failure never blocks boot. Containment
inverts that in prod for one specific case: if the quarantine sweep **cannot run at all** (system DB unreachable),
prod must **not** proceed to `DBOS.launch()` and recover uncontained workflows. Booting with recovery live and
containment dead is precisely the failure mode. Recommendation: prod fails closed on a *total* sweep failure; a
*per-workflow* classification failure is fail-soft (that workflow goes to class E and boot continues). Dev stays
fail-soft throughout. **This is a judgment call worth Fazal's confirmation** — it trades availability for send safety,
and this design's bias is send safety.

### Prerequisites to verify before prod (do not assume — none of these were checkable within this design's read-only
boundary)

1. **`DBOS__VMID` must be set per replica before prod scales past one.** Without it `executor_id` defaults to
   `"local"` (`_utils.py:16`, `_dbos.py:378`). Two replicas both claiming `"local"` will *both* recover the same
   PENDING workflow — concurrent double-execution of an effectful workflow. Single-replica prod is safe; scaling is
   not, and nothing currently guards it.
2. **`DBOS_CONDUCTOR_KEY` presence changes the entire recovery model.** If set, local startup recovery is disabled
   (`_dbos.py:605`) *and* `executor_id` becomes a fresh UUID per process (`_dbos.py:532`). The pre-launch sweep still
   works (it is independent of launch), but the recovery *driver* becomes Conductor and the containment contract must
   be re-verified against it. Check with `scripts/env_presence.py` — **never `railway variables`** (Rule #18 /
   CL-431).
3. **Tenant deletion destroys the effect ledger.** `send_idempotency_keys.tenant_id REFERENCES tenants ON DELETE
   CASCADE` (`migrations/049:25`). Once a tenant is deleted, its effect-state is **unrecoverable** — for class A we
   can never answer "did anything go out?". ⇒ Prod tenant deletion must quarantine that tenant's in-flight workflows
   **and snapshot their effect-state before the delete**. This is the prod analog of dev #53's harness teardown, and
   it belongs in this row's scope because without it class A is permanently undiagnosable. See §6.5.

### Never automated in prod

- Any `resume_workflow` or `fork_workflow`, on any class, under any condition.
- Any remainder send. A human authorises it, every time.
- Any resolution of a `sending` marker.
- Any owner-facing message about a half-sent campaign (pending §6.1).
- Any bulk disposition of class D or E.

**The complete automated scope in prod is: detect → classify → snapshot → cancel → record → alert.** Everything past
"alert" is a human. That is the spec's boundary ("the system CONTAINS + DIAGNOSES + REPORTS — it does not silently
auto-resolve an effectful failure") and this design does not widen it.

---

## §6 — Open decisions (Fazal)

These are product / legal / money calls. The design is built so each can be answered later without restructuring —
but **none should be invented by an implementer.**

**6.1 — What is the owner told about a half-sent campaign, and when?** The core one. Options: (a) told immediately
and offered the remainder; (b) told only after a VTR reviews; (c) not told, VTR completes the remainder silently. The
spec forbids "silently dropped" but does not say who speaks first. Bears on trust, on the Manager's single-voice
principle, and on what a "campaign sent" claim means. *Until answered, the console escalates and composes nothing.*

**6.2 — Who may authorise a remainder run?** Any assigned VTR, or exception tier (Fazal=VTR#1) only? It is a real
customer send from a workflow that already failed once. The existing precedent splits both ways: `force_l3` is
exception-tier, freeze/demote/revoke are open to any assigned VTR.

**6.3 — Default posture for an indeterminate unit.** Today VT-423 implies "blocked forever, no resolver". Options:
(a) treat as SENT after operator review — never re-send, owner told "some may not have gone through"; (b) hold for
delivery evidence indefinitely. (b) is safest and is what the code does now; (a) is more honest to the owner. This
determines whether "hold forever" is an acceptable steady state.

**6.4 — Retention of quarantine records.** DBOS purges workflow inputs at 2h for privacy (`dbos_purge.py:57-62`).
The quarantine snapshot is redacted (ids/statuses/counters/exception classes) but is a longer-lived record of a
tenant's failed work. How long do we keep it? Audit value vs DPDP minimisation.

**6.5 — May a prod tenant be deleted while it has in-flight effectful work?** Options: (a) the delete blocks until
those workflows drain or are contained; (b) the delete proceeds and pre-quarantines them, snapshotting effect-state
first. (a) is safer, (b) is operationally simpler and may be forced by a DPDP erasure deadline. Note the two
interact: a DPDP erasure request may not be blockable.

**6.6 — Alert routing and severity for a prod containment event.** Page Fazal, or land in the ops queue? Suggest
splitting: class D/E page (money-adjacent, ambiguous effect); class A/B/C queue. Needs confirming against the
existing severity map (`alerts/triggers.py:62-88`).

**6.7 — Fail-closed boot in prod** (the §5 asymmetry). Confirm: should prod refuse to boot when the quarantine sweep
cannot run at all? This design recommends yes and biases to send-safety over availability, but it is a real
availability trade and Fazal owns it.

---

## §7 — Scope notes for the build

**In scope for VT-634:** the classifier + registry; the pre-launch quarantine sweep; the quarantine store + its
migration; the steady-state re-sweep including the missing VT-423 stale-`sending` reconciler; the cross-tenant
console page + its actions; the alert kinds; the `max_recovery_attempts` tightening (§0.5); fixing the incorrect
app_version claim in `orphan_reaper.py:8-11` (§0.4).

**Explicitly NOT in scope:** any change to the send gates (opt-out / consent / caps / registry stay exactly where
they are); any new template or SID; any change to VT-420/423 semantics — this design *consumes* them, it does not
revise them; any change to the existing VT-481/525/552/557/560/668 reapers, which heal *application* rows and are a
different mechanism operating after launch; dev #53's harness teardown, which is harness-specific and does not cover
this (per the row).

**Build order that respects the dependencies:** registry + classifier first (it is pure and unit-testable against
fixtures); then the quarantine store; then the snapshot-before-cancel sweep (§0.6 is the thing to get right, and it
wants a test that proves the snapshot survives a GC pass); then the console read surface; then the actions, gated
behind §6.2. The remainder-run action lands last, after §6.1 and §6.2 are answered — it is the only piece that can
send a real message.

**Canary (Rule #15):** the sweep touches the DBOS system DB, which is external persistence. Canary acceptance must
drive a real orphan — construct a workflow whose anchor is then deleted, restart, and prove: (1) it is classified A,
(2) the snapshot lands *before* the cancel, (3) `DBOS.launch()` does not recover it, (4) a subsequent `dbos_purge`
pass deletes the DBOS row while the quarantine record survives intact. Dev has 18 real orphans available as
fixtures — **do not run this against the dev instance while a measurement pack is in flight.**
