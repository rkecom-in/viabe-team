# VT-374 — VTR Run-Control Substrate (Phase A, CL-435) — PLAN v2

**Author:** Cowork (acting architect, CL-435). **v1 GATE-FAILED** (adversarial review,
2026-06-11): the v1 interceptor wrapped post-hoc recording sites (controls nothing) and
v1 fork relied on DBOS `fork_workflow` semantics that are inexpressible at VTR-step
granularity on the live paths. **v2 architectural decision (Cowork): app-level run
control.** No DBOS re-architecture, no DBOS fork. All 14 gate findings closed below
(marked F1–F14).

## 0. The product ask (unchanged)

n8n-style step-level control: see step I/O; pause; override future steps; edit inputs and
re-run from a step; pre-set overrides on upcoming runs. Phases: A substrate (this row),
B read-only canvas (VT-375), C interactive controls (VT-376), D multi-VTR (VT-377).

## 1. The v2 architecture in one paragraph

"Steps" the VTR controls are **application-level steps** — the meaningful units (discover
→ compose-questions → draft → approve → send; evaluate-trial → notify; generate-plan →
deliver), not LLM callback micro-events. A **step registry** declares every step in one of
two honest tiers: **`controllable`** (a true pre-execution call seam exists; pause +
override + re-run-from apply) and **`observed`** (timeline display only; the panel labels
them non-controllable — F1). Re-run is **app-level re-dispatch**: a NEW run with fresh
identity, lineage stamps, pinned context, re-entering every gate (F2/F3). The LLM brain's
internal turns are observed-only by design — controlling inside an agentic loop is neither
expressible nor desirable (F1/F4).

## 2. Binding invariants (unchanged from v1, plus three new)

- **I1 (CL-425/426):** VTR-visible I/O is de-identified; raw = VTR#1 audited exception tier.
- **I2 (Pillar 7):** re-runs NEVER inherit approvals; send/opt-out/consent/approval gates
  structurally non-overridable; customer-visible artifacts re-enter owner approval.
- **I3:** control tables tenant-scoped, RLS+FORCE, `_PURGE_ORDER` same-migration, audited.
- **I4:** migration numbers via the allocator. **I5:** real-DB RLS/purge acceptance.
- **I6 (F5):** opt-out/DSR/consent processing is **pause-EXEMPT by construction**.
- **I7 (F6):** overrides are per-step KEY-allow-listed; no pin may carry customer-visible
  content or customer-identity fields past the owner's eyes.
- **I8 (F11):** every workflow_kind carries a side-effect policy for re-runs (reuse /
  re-emit-under-new-identity / forbidden); violations refuse, not no-op.

## 3. STEP-0 — CC verifies before building

1. **Controllable-seam inventory (replaces the v1 DBOS-fork check):** enumerate the real
   pre-execution call seams on the live paths and propose the initial registry. Expected
   set (verify, don't assume): coordinator work-item dispatch boundary (`execute_item`
   call site); sales-recovery executor sub-steps (candidate build → compose → batch
   create); auto-discovery per-source steps; question-brain compose; plan generate +
   deliver parts; trial sweep evaluate/notify per tenant. Everything else (brain turns,
   direct handlers, journey gate) = `observed` tier, labeled.
2. **Envelope PII audit per step_kind** (F7): writers redact patterns but `name_registry=None`
   — names survive in `think_text`/`decision_rationale`/`action_args.summary`. Classify
   each step_kind name-free vs not; drives §6 view posture.
3. **Side-effect inventory per workflow_kind** (F11): L2 episodic (uuid5 over run_id), KG
   outbox, twilio idempotency keys, work-item status writes — classify per I8.

## 4. Component 1 — controllable-step executor (`orchestrator/run_control.py`)

At each **controllable** seam, the call site routes through:

```
run_controlled_step(reg_entry, ctx, *, tenant_id, run_id, workflow_kind) -> StepResult
```

1. **Pause check** (boundary-only, durable-safe — F4): performed at controllable seams,
   which sit in plain pre-execution code OUTSIDE the LLM loop; where a seam lives inside a
   DBOS workflow body, the pause wait is implemented as a checkpointed wait (`@DBOS.step`
   poll or `DBOS.recv`-based release event), never a bare in-loop sleep mid-brain.
   Mid-brain pause is explicitly out of scope.
   **Two-tier failure semantics (F9):** `/pause` succeeds only after a verifying read-back;
   once a pause is ACKNOWLEDGED for a (tenant, kind) scope, control-read errors fail
   CLOSED for that scope (per-process last-known-state cache); scopes with no known pause
   fail OPEN on read errors + raise a `run_control_degraded` alert. Acceptance covers both.
   **Pause exemption (F5/I6):** the opt-out/DSR fast path runs BEFORE any pause hold;
   `pre_filter` + direct handlers (opt-out, DSR) are in a pause-deny-list mirroring the
   override deny-list. Acceptance: a fully-paused tenant still processes "STOP" end-to-end.
2. **Override consult — consume-first (F8):** `SELECT ... FOR UPDATE SKIP LOCKED` on the
   matching unconsumed, unexpired row; stamp `consumed_at` + `consumed_run_id` in that
   SAME txn BEFORE execution. NULL-workflow_id (next-run) overrides REQUIRE `expires_at`
   (default 7 days; sweep cancels expired). Two racing runs: one consumes, one proceeds
   clean. Acceptance races them.
   - `pinned_input`: deep-merge restricted to the registry's **allowed_keys** for that
     step (F6/I7). Keys carrying customer-visible content or customer-identity bindings
     are never allow-listed; an attempt → 422 with the registry reason.
   - `pinned_output`: legal ONLY for steps registered `pure_return=True` (no DB-mediated
     effects — F6 scenario A); otherwise 422.
3. **Execute + record** as today; the step record additionally stores
   `override_id`/`paused_ms` so the timeline shows what was controlled.

**Deny-list from a canonical manifest (F14):** `run_control/gate_manifest.py` lists the
gate modules (customer_send, pre_filter_gate, consent helpers, approval_resume,
customer_inbound, transitions money edges, twilio send fns). Registry import raises if any
manifest module appears as controllable; a CI grep-test (Pillar-1 pattern) fails when a
new send/consent module exists outside the manifest.

## 5. Component 2 — tables (ONE migration via allocator)

- **`step_overrides`** — as v1 PLUS: `expires_at` NOT NULL for workflow_id-NULL rows (F8);
  `pinned_input`/`pinned_output`/`reason` are **redacted at WRITE** through `pii_redactor`
  WITH the tenant's customer-name registry injected; `reason` length-capped (F7).
- **`workflow_controls`** — as v1 (paused rows carry set_by/reason/released_*).
- **`pipeline_runs`** + `rerun_of_run_id uuid NULL`, `rerun_from_step text NULL` (F3
  lineage; no DBOS workflow-id dependency).
- Both new tables: RLS+FORCE, `_PURGE_ORDER` same migration, zero `app_vtr_role` direct
  grants. Documented as PII-at-rest surfaces (purge keyed on tenant) (F7).

## 6. Component 3 — `vtr_step_timeline` view (F7 posture = Gap-6 precedent)

Default: structural fields (run lineage, step seq/name/status/timing/override_id) +
envelope **KEYS ONLY** (the mig-130 `diff_from_prev` pattern — read-time redaction of free
text was already rejected there). Envelope VALUE passthrough only for step_kinds the
STEP-0 §3.2 audit proves name-free, enumerated in the view definition. Raw envelopes stay
exception-tier via the EXISTING Gap-6 audited admin path. Real-DB test: `app_vtr_role`
cannot read raw tables; value-passthrough list matches the audit.

## 7. Component 4 — run-control ops API (Gap-6 auth pattern)

`/pause` `/release` `/override` `/cancel-override` `/rerun` `/timeline/{run_id}`.
Auth: internal secret + VTR JWT (exp required) + assignment gate. **IDOR posture stated
precisely (F12):** row-targeted mutations derive tenant from the TARGET row; tenant-scoped
mutations (pause, next-run overrides) take tenant from the request and REQUIRE the
operator↔tenant `require_vtr_action` assignment gate (all-tenants for VTR#1 admin tier).
Audit row before every mutation.

## 8. Component 5 — re-run (`rerun_from`) — app-level re-dispatch (F2/F3/F10/F11)

`rerun_from(source_run_id, from_step, overrides=[]) -> new_run_id`:
1. **409 while the tenant has ANY open `pending_approvals`** (F10 — the owner's YES must
   never be ambiguous).
2. Mint a FRESH app run identity (new uuid4 + a `rerun` salt on any uuid5-derived child
   identities; never the source sid/work-item uuid5 — F3). Stamp lineage columns.
3. Pre-register the supplied overrides bound to the new run_id.
4. Re-dispatch the workflow_kind's app entry point (work-item re-dispatch for agent runs;
   plan regeneration for spine runs; discovery re-run for auto-discovery). Prior steps are
   NOT replayed-from-history: steps before `from_step` re-execute ONLY if the entry point
   requires them, and the registry records per-kind which prefix steps are skippable via
   pinned context. This is re-dispatch, not time-travel — the plan says so honestly, and
   the panel copy must too.
5. **Side-effect policy enforced per I8/F11:** L2 episodic + KG outbox emit under the NEW
   run identity; effects classified `forbidden-on-rerun` cause `/rerun` to refuse for that
   kind; the forked run never mutates the source run's rows (acceptance asserts source
   timeline untouched).
6. Every gate re-evaluates; approvals are never inherited (the §10.6 test from v1 stands:
   re-run past a draft step → NEW batch in `drafted` + NEW approval; `agent_send_draft`
   still refuses on the consent empty-set stop).

## 9. Component 6 — step harness CLI (F13 amendments)

As v1 (stub-default, `--live` refuses deny-listed steps + non-dev DB via the VT-362
sentinel) PLUS: the registry records `inputs_redacted_at_write: bool` per step; the
harness prints a loud warning when replaying redacted envelopes (the replay is
unrepresentative for body-consuming steps); the harness is a CC/Fazal OPS tool only —
never a VTR-facing surface (it reads raw envelopes via DB creds, bypassing I1).

## 10. Acceptance (all real-DB where DB-touching; live-path where workflow-touching)

1. RLS+FORCE on both tables; `app_vtr_role` zero raw reads; purge hard-delete canary.
2. Pause on the LIVE `webhook_pipeline_run` path (not a synthetic toy — F1/F4): paused
   tenant holds at the controllable boundary, survives worker restart (checkpointed wait),
   resumes on release with correct replay semantics.
3. **Paused tenant still processes STOP/DSR end-to-end (I6/F5).**
4. Override: consume-first race test (two concurrent matching runs → exactly one
   consumes); expiry sweep; allowed-keys 422; pure_return-only pinned_output 422;
   deny-list rejection at API + import-time + CI grep (F14).
5. Acknowledged-pause fail-CLOSED + ambient fail-OPEN + degraded alert (F9).
6. `rerun_from`: fresh identity, lineage stamped, source run untouched, 409 on open
   approval (F10), approval non-inheritance, side-effect policy (L2/outbox under new
   identity; forbidden-kind refusal) (F11).
7. `vtr_step_timeline`: keys-only default; value-passthrough exactly the audited list;
   redacted-at-write pinned_*/reason (write a name + phone into reason → stored redacted).
8. Harness stub/`--live`/redaction-warning behaviors.
9. gitleaks-safe; 3 real gates green (ci-success + migrations + orchestrator).

## 11. Merge class + sequencing

Risk row (core runner seams + RLS + PII-adjacent tables): this plan is the plan-first
artifact; Cowork adversarial gate on BUILT code with EXECUTED evidence; Cowork-authorized
dev merge under CL-435. One coherent PR. VT-378 (signup styling) may interleave. B→C→D
serial. **Phase-B/C copy obligation:** the panel must label observed-only steps and
describe re-run as re-dispatch (no time-travel claims) — honesty is part of the product.

## 12. Gate-round-2 amendments (MANDATORY — conditions of the GATE-PASS)

- **N1 — VT-300 `run_controls` relationship.** A run-control substrate ALREADY exists:
  `run_controls` + `run_control_handler.consume_pending_control`, consumed at the
  supervisor campaign-send fan-out (`supervisor.py:200-203`). STEP-0 adds a full inventory
  of it. Design ruling: **`workflow_controls`/`step_overrides` SUPERSEDE `run_controls`
  semantics going forward IF the inventory shows run_controls is single-purpose
  (campaign-send hold); in that case the supervisor seam migrates onto the new substrate
  in THIS row and `run_controls` is retired in the same migration. If the inventory shows
  broader live usage, COEXIST: the supervisor keeps run_controls, the timeline view must
  surface run_controls holds so the panel never shows "not paused" while a VT-300 hold is
  active.** CC reports the inventory + which arm applies in the plan-ack; retire-vs-coexist
  beyond that scope → STOP + signal.
- **N2 — recovery-idempotent consume.** The override match predicate is
  `(consumed_at IS NULL OR consumed_run_id = :current_run_id)` — DBOS recovery re-running
  a workflow body after the consume-txn committed must re-apply the SAME override, not
  silently proceed without it. Acceptance adds a kill-and-recover case with a pinned
  override (override still applied on the recovered run).
- **N3 — the webhook-path controllable seam, named.** The pre-`dispatch_brain` call site
  (`runner.py:591-598`) is registered as a **pause-only** controllable boundary (no
  overrides there). Acceptance #2 tests THAT seam. Concurrently-held inbound runs release
  with NO ordering guarantee — stated in panel copy.
- **F10 note.** The real approval-ambiguity guarantee is migration-128's
  `pending_approvals_one_open_per_tenant` partial-unique + `request_owner_approval`
  step-0b refusal; the `/rerun` 409 is UX on top of that structural guarantee, and the
  acceptance asserts the structural layer (a rerun racing an approval-create converges to
  refusal, never two open approvals).
- **N4 note.** "Acknowledged pause fails closed" holds per-process; the cache is empty on
  boot, so a post-restart control-read error fails OPEN (narrow: control-table-specific
  errors only). Warm the cache from `workflow_controls` at worker boot (best-effort) and
  keep the degraded alert; the guarantee is stated as best-effort-after-restart.
- **N5 note (product ceiling, accepted).** Keys-only visibility makes some Phase-C edits
  blind-writes for non-audited step_kinds. Accepted: I1 is binding and allowed_keys are
  config/ID-class by I7; VTR#1's audited exception tier covers the single-VTR phase;
  Phase-D multi-VTR inherits the keys-only ceiling. Panel copy discloses keys-only
  visibility (added to the §11 copy obligation).
