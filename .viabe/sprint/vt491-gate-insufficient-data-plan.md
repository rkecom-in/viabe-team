---
id: VT-491
title: "self_evaluate must ACCEPT the CampaignPlanInsufficientData variant (deterministic detect-before-grade)"
status: Plan-ready
priority: High
type: BUILD
area: team-orchestrator
created: 2026-06-29
investigated: 2026-06-29
investigator: CC (read-only — code-grounded, runs 44f12ad2 / 0844b0ff)
risk: "money-path quality gate — plan-first, Cowork gates before build"
authorization_basis: "Final win-back re-drive defect B: gate mis-handles insufficient_data (non-deterministic PASS/REVISE)."
---

# VT-491 — self_evaluate insufficient_data handling: diagnosis + fix plan

## TL;DR

`SelfEvaluateGate.run()` (`agent/self_evaluate.py:252`) hands **every** draft to the
LLM grader — there is **no variant check**. The grader prompt
(`tools/prompts/self_evaluate_v1.md`) is written **entirely to grade a `proposed`
CampaignPlan**; it has zero instruction for the legal `insufficient_data` /
`out_of_scope` terminals. So when SR correctly emits `CampaignPlanInsufficientData`,
the LLM's behaviour is **undefined → non-deterministic**: it PASSed on run `44f12ad2`
and REVISE'd on run `0844b0ff` (with a *factually wrong* critique — see §1).

**Fix:** detect the non-proposed variant **deterministically (isinstance, no LLM)**
at the TOP of `SelfEvaluateGate.run()` and **short-circuit to ACCEPT** before any
grading. A real `proposed` plan still gets the full unchanged four-category grade.
The downstream data-remediation routing **already exists** (`collapse_node` →
`record_terminal_verdict`); the bug is purely that the gate corrupts the plan *en
route* to it. **No prompt edit** — determinism comes from the isinstance check, NOT
from teaching the LLM a new branch (that would re-introduce non-determinism).

---

## 1. ROOT CAUSE (file:line)

### 1a. The gate grades all three variants — no variant gate

`SelfEvaluateGate.run(self, draft: CampaignPlan)` —
`apps/team-orchestrator/src/orchestrator/agent/self_evaluate.py:252-310`:

```
self.tool_counter.record_dispatch()          # :268  — charges a tool-budget slot
...
verdict = self.evaluator.evaluate(draft, EVALUATION_CRITERIA)   # :275 — LLM call, ALWAYS
```

There is **no `isinstance(draft, CampaignPlanProposed)` guard**. The module imports
only `CampaignPlan` + `SelfEvaluateStatus` (`:46-49`) — it never references the leaf
variant classes. Every variant the union can carry (`proposed`, `out_of_scope`,
`insufficient_data`) is forwarded to the Opus seam identically.

### 1b. The unconditional caller

`run_sales_recovery_agent` — `agent/sales_recovery.py:641,662`:

```
draft_plan = parse_campaign_plan(output)   # :641 — any of the 3 variants
...
gate_outcome = gate.run(draft_plan)        # :662 — runs the gate regardless of variant
```

`parse_campaign_plan` happily returns a `CampaignPlanInsufficientData`
(`campaign_plan.py:366-371`), and the caller pushes it straight into the gate.

### 1c. The grader prompt is proposed-only

`tools/prompts/self_evaluate_v1.md` — the four categories
(`schema` / `pillar` / `consistency` / `legal`, `:24-73`) reference **proposed-only
fields** exclusively: `target_cohort.cohort_size`, `expected_arrr.basis`,
`message_plan.template_params`, `evidence_refs`. The prompt's only mention of a
variant is *"`evidence_refs` empty on a `proposed` variant"* (`:31`). It has **no
instruction** for `insufficient_data` (whose only payload is `missing_data:
list[MissingDataItem]`, `campaign_plan.py:366-371`). Handed
`{"status":"insufficient_data","missing_data":[...]}`, the model is off-contract:

- **run 44f12ad2 → PASS** (no proposed fields present to fault → "looks clean").
- **run 0844b0ff → REVISE** with
  `schema_critique: "status is 'insufficient_data' — a CampaignPlan must have status
  'proposed'/'approved'/'active', not a data-availability state."`
  This critique is **itself wrong**: `insufficient_data` **is** a legal
  `CampaignStatus` (`campaign_plan.py:74`), and `approved`/`active` are **not even on
  this contract** (the contract is exactly `proposed` / `out_of_scope` /
  `insufficient_data`, `campaign_plan.py:69-74` — lifecycle states live downstream).
  The model invented states that don't exist.

**Both verdicts are wrong** because the gate is asking an LLM to grade a thing that is
not a plan. The defect is the *missing deterministic guard*, not a tunable prompt.

### 1d. Why the non-determinism is harmful (per-verdict downstream consequence)

The correct terminal routing for a non-proposed plan already exists:
`collapse_node` (`collapse.py:315-379`) dispatches `proposed` → campaign write +
approval, and `out_of_scope`/`insufficient_data` → `record_terminal_verdict`
(`collapse.py:202-282`), which writes one `pipeline_steps` row
(`step_kind='campaign_plan_emitted'`, `variant='insufficient_data'`, `missing_data`
in the envelope) and **no campaign, no send**. That **is** the "no campaign possible
yet, here's the missing data" data-remediation terminal.

The gate sits *upstream* of collapse and corrupts the plan before it ever gets there:

| LLM verdict on insufficient_data | Gate action (`self_evaluate.py`) | Result | Verdict |
|---|---|---|---|
| **PASS** (44f12ad2) | SHIP (`:283-289`) → `sales_recovery.py:675-681` stamps + `status='completed'` | plan reaches `collapse_node` → `record_terminal_verdict` intact | **accidentally correct** |
| **REVISE 1st** (0844b0ff) | RETRY (`:304-310`) → appends *"status must be proposed"* feedback, loops | model re-drafts: may **fabricate a `proposed` plan** to satisfy the gate (Pillar-7 violation), re-emit insufficient_data (burns budget), or emit non-dict terminal → `agent_terminal_no_dict` → RuntimeError → run stuck `running` (the **VT-492** path) | **wrong** |
| **REVISE 2nd** | REJECTED (`:295-302`) → `sales_recovery.py:696-713` routes `SELF_EVAL_REJECTED`, `status='rejected'` | a legitimate "not enough data" terminal becomes a **rejected run paging Fazal** (`_emit_self_eval_rejected`, router default `ESCALATE_TO_FAZAL`) | **wrong** |

So the observed non-determinism is "accidentally-correct OR escalate-Fazal OR
fabricate-a-plan", entirely at the mercy of an off-contract LLM coin-flip.

**Same bug class affects `out_of_scope`** (also non-proposed, also goes through the
gate). The fix covers both variants; `insufficient_data` is the launch-blocking one.

---

## 2. THE FIX — deterministic detect-before-grade (no LLM, no prompt change)

### 2a. Primary change — variant short-circuit at the top of `SelfEvaluateGate.run()`

`agent/self_evaluate.py` — add the leaf-variant import and a deterministic guard as
the **first statement** of `run()`, *before* `record_dispatch()` and *before* the
evaluator call:

```python
# import (extend the existing campaign_plan import block, :46-49)
from orchestrator.agent.schemas.campaign_plan import (
    CampaignPlan,
    CampaignPlanProposed,   # ADD
    SelfEvaluateStatus,
)

def run(self, draft: CampaignPlan) -> GateOutcome:
    # VT-491: the quality gate grades PROPOSED plans only. out_of_scope /
    # insufficient_data are legal terminal verdicts (a refusal / a "no campaign
    # possible yet, here's the missing data" state) — there is nothing to grade.
    # ACCEPT them deterministically (isinstance, no LLM) and route them onward
    # unchanged. No record_dispatch: no Opus call happens, so no tool-budget slot
    # is charged and no cost accrues.
    if not isinstance(draft, CampaignPlanProposed):
        return GateOutcome(
            action=GateAction.SHIP,
            self_evaluate_status=SelfEvaluateStatus.NOT_APPLICABLE,  # see §2c
            attempt_number=0,      # 0 = no grading attempt (observability marker)
            outcome=None,
        )

    self.tool_counter.record_dispatch()   # existing :268, now proposed-only
    ...                                   # rest of run() UNCHANGED
```

**Why the gate, not the caller:** the gate is the quality-grade authority; its
contract becomes honest ("I grade proposed plans; non-proposed terminals are accepted
as-is"). Keeping the variant logic in ONE place (the gate) — rather than scattering
an `isinstance` skip into `run_sales_recovery_agent` — mirrors the existing
single-dispatch discipline in `collapse_node` (the only place that branches on variant
identity). **Recommend gate-only**; do NOT also add a caller-side skip (avoid two
sources of truth).

**Why before `record_dispatch()`:** a deterministic accept makes **no** model call, so
it must not consume one of VT-35's 25 tool-budget slots nor accrue cost
(`gate.evaluator_calls` stays 0 → `AgentResult.tool_calls_made` unaffected,
`sales_recovery.py:735-736`). Reaching a terminal draft implies the loop was not
already cancelled (a prior hard-limit cancel breaks the loop before the gate,
`sales_recovery.py:510,540`), so skipping the gate's internal cancel check for the
short-circuit is safe. **[Design point for Cowork:]** confirm "deterministic accept is
free / uncounted" vs. the alternative of charging a dispatch for parity — recommend
free, because no Opus call occurred.

### 2b. The downstream routing is already correct — nothing to build there

With the short-circuit returning **SHIP**, `run_sales_recovery_agent` (`:675-681`)
stamps `self_evaluate_status` and breaks with `status='completed'`, `output` = the
**unchanged** insufficient_data plan. `_sales_recovery_node` (`supervisor.py:144-157`)
re-parses it into `CampaignPlanInsufficientData` and attaches it to
`state['campaign_plan']`; `collapse_node` (`collapse.py:376-379`) routes it to
`record_terminal_verdict` (the data-remediation terminal: surface `missing_data`, no
campaign, no send). **No new routing code** — the fix simply stops the gate from
mangling the plan before collapse sees it.

### 2c. `self_evaluate_status` for the short-circuit — small enum decision

The status field is **cosmetic for non-proposed** variants: `record_terminal_verdict`
(`collapse.py:235-261`) reads `variant` + `missing_data`, **never**
`self_evaluate_status`. Two options:

- **Option A (recommended): add `SelfEvaluateStatus.NOT_APPLICABLE = "not_applicable"`**
  (`campaign_plan.py:84-88`, one line). Semantically precise — the quality grade does
  not apply to a non-proposed terminal; distinguishable in observability from a graded
  plan. Tiny enum widening, nothing asserts on it downstream.
- **Option B (minimal): leave the `GateOutcome` default `NOT_YET_EVALUATED`** — zero
  schema change, but "not *yet*" implies a grade is pending that will never come.

Recommend **A** for honest observability; flag it for Cowork as a (trivial) schema
touch. Either way the data-remediation routing is identical.

### 2d. Explicitly NOT changing `self_evaluate_v1.md`

The prompt stays proposed-only. Because the short-circuit means the LLM **never sees**
a non-proposed variant, the prompt needs no "if insufficient_data, accept" branch — and
we deliberately do NOT add one: an LLM-judged branch is exactly the non-determinism
source we are removing. **Proposed plans get the full, unchanged four-category grade —
the gate is not weakened for real plans.**

---

## 3. INTERACTION WITH VT-490 AND VT-492

### VT-490 (SR cohort-surfacing — the PRIMARY win-back fix)

- VT-490 surfaces the dormant cohort `customer_ids` into the SR context so SR can
  ground a **`proposed`** plan instead of correctly returning `insufficient_data`.
  After VT-490, the **happy path** emits `proposed` → the gate grades it fully (as
  designed). VT-490 makes `insufficient_data` **rarer on the win-back path**.
- VT-491 is **still required and orthogonal**: `insufficient_data` remains a **legal
  terminal** SR must be able to emit whenever data genuinely IS missing (a tenant with
  no dormant cohort, a partial onboarding, a trigger that fires before data lands).
  VT-490 does NOT eliminate the variant; it only narrows when it occurs. The gate must
  accept it deterministically **regardless of whether VT-490 has landed**.
- **Different files, no collision:** VT-490 = `context_builder.py` cohort surfacing;
  VT-491 = `self_evaluate.py` (+ maybe the `campaign_plan.py` enum). Land independently,
  any order. VT-491 is the safety net for the genuine-missing-data case.

### VT-492 (SR-node invalid/no-dict terminal robustness)

- VT-491 **removes one upstream trigger** of VT-492's stuck-`running` path: today a
  REVISE on `insufficient_data` → RETRY → the model re-drafts and (on 0844b0ff) emitted
  non-dict terminal text → `agent_terminal_no_dict` → RuntimeError → run orphaned at
  `running`. With VT-491, `insufficient_data` never triggers a REVISE/RETRY, so **that
  particular path to the non-dict terminal disappears**.
- They are **independent and both needed**: VT-492 is the *general* robustness fix —
  **any** invalid/no-dict terminal (including a genuinely malformed `proposed` retry)
  must close the run cleanly, never orphan `running`. VT-491 does not make VT-492
  unnecessary; it just deletes one class of spurious retries that fed it.
- **Different files, no collision:** VT-491 = the gate; VT-492 = the SR-node /
  run-close terminal path. **Recommended order: VT-491 first** (removes the spurious
  insufficient_data retries) **then VT-492** (catches whatever invalid terminals
  remain). No hard dependency either way.

---

## 4. TEST PLAN

All CI-safe — `FakeSelfEvaluator` (scripted/raising) + the existing `_patch_anthropic`
harness in `tests/orchestrator/agent/test_self_evaluate_gate.py`. **No API quota.**
The crux assertion is **`FakeSelfEvaluator.calls == 0`** on non-proposed variants — it
*proves* determinism (the LLM seam is never consulted, so the verdict cannot vary).

**Unit — `SelfEvaluateGate.run()` directly:**

1. **insufficient_data → deterministic ACCEPT, NO grade.** Build a
   `CampaignPlanInsufficientData` (`status=insufficient_data`, `missing_data=[...]`).
   Construct the gate with `FakeSelfEvaluator(raise_on_call=AssertionError("seam must
   not be called"))`. Assert: `run(plan).action is GateAction.SHIP`;
   `self_evaluate_status is NOT_APPLICABLE` (or `NOT_YET_EVALUATED` per §2c);
   `evaluator.calls == 0`; `tool_counter` **not** incremented (no dispatch charged);
   `attempt_number == 0`. This is the direct regression for runs 44f12ad2 / 0844b0ff.
2. **out_of_scope → same deterministic ACCEPT** (sibling non-proposed variant), seam
   not called. Covers the same bug class.
3. **proposed plan → STILL fully graded (not weakened).** Valid `CampaignPlanProposed`
   + `FakeSelfEvaluator([PASS])`. Assert: SHIP; `self_evaluate_status == PASSED`;
   `evaluator.calls == 1` (the seam **was** invoked); `tool_counter` incremented once.
4. **thin/bad proposed plan → STILL REJECTED.** `FakeSelfEvaluator([REVISE, REVISE])`.
   Assert: REJECTED; `FAILED_AFTER_REVISIONS`; `evaluator.calls == 2`. Regression guard
   that the short-circuit does not let a bad proposed plan skip the grade.

**Integration — `run_sales_recovery_agent` gate-on (mocked Anthropic):**

5. Model emits `insufficient_data` (via `_patch_anthropic`), gate wired with a
   raise-on-call `FakeSelfEvaluator`. Assert: `AgentResult.status == 'completed'`;
   `output['status'] == 'insufficient_data'`; `missing_data` preserved verbatim;
   **no** `SELF_EVAL_REJECTED` and **no** `AGENT_INVALID_OUTPUT` routed (spy/patch
   `route_failure`); `evaluator.calls == 0`. The run does not escalate and does not
   retry — it lands deterministically at the terminal-verdict path.
6. **(Optional) collapse integration:** feed the resulting plan through `collapse_node`
   → assert `record_terminal_verdict` writes one `campaign_plan_emitted` row with
   `variant='insufficient_data'` + `missing_data` (the data-remediation terminal), and
   **no** `campaigns` row / no approval request. Confirms end-to-end routing intact.

---

## 5. SCOPE / FILES TOUCHED (one coherent PR)

- `apps/team-orchestrator/src/orchestrator/agent/self_evaluate.py` — import
  `CampaignPlanProposed`; add the isinstance short-circuit at the top of
  `SelfEvaluateGate.run()`.
- `apps/team-orchestrator/src/orchestrator/agent/schemas/campaign_plan.py` —
  **(Option A only)** add `SelfEvaluateStatus.NOT_APPLICABLE`.
- `apps/team-orchestrator/tests/orchestrator/agent/test_self_evaluate_gate.py` —
  tests 1-5 (and optional 6 in `test_collapse.py`).
- **NOT touched:** `self_evaluate_v1.md` (prompt stays proposed-only),
  `sales_recovery.py` (no caller-side variant skip — gate owns it), `collapse.py`
  (routing already correct).

**Risk:** money-path quality gate. The change is *narrowing* — it removes an
off-contract LLM call and replaces it with a deterministic accept; it cannot make a
real `proposed` plan pass-when-it-should-reject (tests 3-4 lock that). Plan-first per
the standing money-path discipline; Cowork gates before build.
