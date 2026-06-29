---
id: VT-500
title: "Calibrate the campaign-grounding gate to CLAIMS + SCALE (simple-tier win-back ships without a defensible ARRR)"
status: Plan-first (read-only) — awaiting Cowork gate
priority: Critical
type: BUILD
area: team-orchestrator
created: 2026-06-30
authorized_by: fazal
authorization_basis: "Fazal: grounding required to SHIP must be proportional to what the campaign CLAIMS + its scale/cost. A plain no-claim win-back to a genuinely-lapsed consented customer should not need a defensible ARRR projection; an offer/large/money-bearing campaign still must."
branch: cc-winback-followups
head_at_plan: c7e9e3f  # prompt said 08cc697 — that is an ANCESTOR of HEAD (c7e9e3f = the VT-500 docs commit). No drift.
---

# VT-500 — calibrate self_evaluate to CLAIMS + SCALE (plan-first, read-only)

## 0. Reconciliation (Rule #14)

- Prompt anchored "branch cc-winback-followups at 08cc697". Actual HEAD = `c7e9e3f`.
  `git merge-base --is-ancestor 08cc697 HEAD` → **YES**: `08cc697` is one commit behind
  `c7e9e3f` (the latter is `docs(sprint): VT-500 Fazal chose calibrate-gate-to-scale`).
  No code drift; the files this plan touches are unchanged between the two.
- Target files confirmed present (prompt's paths were off — real locations):
  - gate: `apps/team-orchestrator/src/orchestrator/agent/self_evaluate.py`
  - prompt: `apps/team-orchestrator/src/orchestrator/agent/tools/prompts/self_evaluate_v1.md`
  - real evaluator: `apps/team-orchestrator/src/orchestrator/agent/tools/self_evaluate.py`
  - schema: `apps/team-orchestrator/src/orchestrator/agent/schemas/campaign_plan.py`
  - registry: `apps/team-orchestrator/config/twilio_templates.yaml` +
    `src/orchestrator/templates_registry.py`
  - context: `src/orchestrator/context_builder.py` (`build_self_evaluate_context_summary`)
  - send floor: `src/orchestrator/agents/send_checkpoint.py` + `agents/autonomy.py`

---

## 1. The PRINCIPLE + what stays untouched

**Principle (Fazal).** The grounding a campaign must defend to SHIP is **proportional to
what it CLAIMS and at what SCALE/COST**. A plain "we miss you" win-back that makes **no
revenue claim**, sent to a **genuinely-lapsed, consented** customer in a **small** cohort,
should NOT be blocked merely because it cannot defend an ARRR/ROI projection. A
large / offer / revenue-bearing campaign **still** must defend the full ROI/ARRR grounding.

This calibrates **exactly one axis: the ROI / `expected_arrr` business-justification
requirement inside the self_evaluate grade.** It is NOT a safety-rail change and NOT an
anti-fabrication change.

### WHAT STAYS UNTOUCHED (explicit — must not drift)

- **Safety rails — UNCHANGED.** consent / opt-out / PII-scrub (VT-498) / onboarded /
  send-checkpoint (VT-474) / caps (VT-460). These live at the SEND path
  (`agents/customer_send.py`, the compliance gates, `send_checkpoint_decision`,
  `autonomy.is_always_confirm`) — **structurally separate from self_evaluate**. The gate
  grades a PLAN; the send path decides send-vs-skip. This calibration does not touch any
  of them. A simple-tier "ship" verdict from the gate still hits the **L2 send-checkpoint**
  (owner approves each send for an unproven tenant / novel template / first-contact) before
  anything leaves.
- **Anti-fabrication grounding — UNCHANGED for BOTH tiers.** The message + plan must still
  ground to bundle facts: no invented customer facts, no invented per-vertical numbers, no
  invented offer, no PII in params, no misleading financial claims. Enforced by (a) the
  self_evaluate `schema` / `pillar` / `consistency`(grounding) / `legal` categories, (b) the
  executor's deterministic `validate_draft_params` literal-bundle grounding
  (`sales_recovery_executor.py:418`), and (c) the pydantic parse-time validators
  (`evidence_refs` non-empty, evidence-marker consistency, `cohort_size == len(customer_ids)`).
  **None of these move.**
- **ONLY the ROI / `expected_arrr` business-justification requirement is calibrated** — and
  only on the narrow simple tier defined below. `expected_arrr` remains a **required schema
  field** (`CampaignPlanProposed.expected_arrr`, `campaign_plan.py:305`) — the simple tier
  still emits a best-effort band (`low/high/confidence/basis`); it is just no longer a
  ship-**blocker** when that band is weakly defensible.

### Why this is the right calibration (the concrete pain)

The win-back re-drive history is the evidence: VT-485 → VT-498 show self_evaluate
**legitimately** REVISE/REJECT a genuinely-lapsed, no-offer win-back because the L4 win-back
attribution baseline retrieval was thin ("L4 retrieval failed … proceeding without" — VT-498),
so the plan could not defend `expected_arrr` plausibility-vs-cohort. That is precisely the
case Fazal wants to ship: a no-claim message to a real lapsed customer should not be gated on
a defensible ARR projection it never needed.

---

## 2. The exact branch point in `self_evaluate.py`

**File:** `src/orchestrator/agent/self_evaluate.py`, method `SelfEvaluateGate.run()`
(starts **line 253**).

The branch attaches cleanly in **one** spot because the draft's type is already proven at
that point:

```
253  def run(self, draft: CampaignPlan) -> GateOutcome:
...
289      if not isinstance(draft, CampaignPlanProposed):   # VT-491 short-circuit (UNCHANGED)
290          return GateOutcome(action=GateAction.SHIP, attempt_number=0, outcome=None)
                                                         # <-- non-proposed terminals exit here,
                                                         #     BEFORE any tier logic. Untouched.
        # ▼▼▼ NEW: tier classification — draft is now a CampaignPlanProposed ▼▼▼
        #     tier = self._classify_tier(draft)
        # ▲▲▲ inserted here (≈ line 299), before record_dispatch()
300      self.tool_counter.record_dispatch()
...
307      verdict = self.evaluator.evaluate(draft, EVALUATION_CRITERIA)   # ← pass tier through
...
315      if verdict.outcome is SelfEvaluateOutcome.PASS: ...            # PASS/REVISE decision
326      self.revisions_used += 1                                       # ← tier filters feed here
```

**How it reads `(template_type, cohort_size)`** — directly off the typed proposed draft, no
new plumbing:

- `template_id = draft.message_plan.template_id`  (`campaign_plan.py:244`, the registry name)
- `cohort_size = draft.target_cohort.cohort_size`  (`campaign_plan.py:178`; schema already
  pins `cohort_size == len(customer_ids)`, so it cannot lie about scale)
- `money_bearing = _resolve_money_bearing(template_id)` — read from the registry, **fail-closed**:
  reuse the exact pattern at `agents/l3_hold.py:252` (`registry_resolve(name,"en").money_bearing`;
  `TemplateRegistryError → True`). An unresolvable template ⇒ treated money-bearing ⇒ strict.

**Tier predicate** (allow-list, fail-closed — NOT a generic "money_bearing == False" relax):

```
SIMPLE  iff  template_id == "team_winback_simple"      # the ONE no-offer customer_marketing winback
        and  money_bearing is False                    # defence-in-depth vs registry drift
        and  cohort_size <= SIMPLE_SHIP_MAX_COHORT      # = L3_AUTO_MAX_BATCH (see §4)
STRICT  otherwise                                       # offer / money / large / any other / unresolved
```

`team_winback_simple` is the only template with `agent_selectable: true`,
`category: customer_marketing`, **no** `money_bearing`, and **no** `offer_description`
param (`twilio_templates.yaml:292`). `team_winback_offer` carries `money_bearing: true` +
`offer_description` (`:307`) ⇒ never simple. The allow-list (vs a property-derived rule)
means a *future* template can't accidentally inherit the relaxed lane.

`WINBACK_TEMPLATE_NAME = "team_winback_simple"` (`sales_recovery_executor.py:151`) and
`L3_AUTO_MAX_BATCH = 20` (`autonomy.py:35`) are imported — **no literals duplicated**.

---

## 3. SIMPLE-tier grade criteria vs OFFER/LARGE full grade

### SIMPLE tier — SHIP criteria

A simple-tier draft **ships iff**, after the grade, every category clears **except that
`expected_arrr`-path critiques are non-blocking**. Concretely, the four things Fazal named map
to where they are enforced — and all but the ARRR axis stay binding:

| Fazal's criterion | Where enforced on the simple tier | Status |
|---|---|---|
| **genuinely lapsed** (recency-grounded, VT-485) | self_evaluate `consistency`: target_cohort cross-checked vs `customer_ledger_summary.recency_days_pctl` + `recency_basis` + the VT-490 dormant-cohort rows in `context_summary` | **BINDING** |
| **message grounded to bundle facts, no fabrication** | self_evaluate `schema` + `pillar`(minus ARRR-confidence) + `consistency`(grounding) + `legal`; executor `validate_draft_params` literal-bundle check; VT-498 personalization scrub | **BINDING** |
| **consented + eligible + opt-out** | **NOT a self_evaluate concern** — enforced UNCHANGED per-customer at the SEND path (consent / opt-out / onboarded gates, VT-460). The grader sees a PII-free `context_summary` with no per-customer consent flags, so it must NOT be asked to assert consent it cannot see. The send path below the gate is the authority. | **BINDING (downstream, unchanged)** |
| **expected_arrr** | best-effort band still emitted (schema-required); the gate does **not** REVISE/REJECT solely on a weak/implausible-vs-cohort ARRR band | **RELAXED — the one calibrated axis** |

**The single relaxation, precisely scoped.** The ROI/ARRR defensibility is NOT a clean
category — it is two *sub-rules* spread across `pillar` ("`expected_arrr.basis` overstates
confidence") and `consistency` ("`expected_arrr.high_paise` implausibly large for cohort").
So the relaxation is **sub-category**, by field-path, not by dropping a whole category
(dropping all of `consistency` would also drop the cohort-grounding check we MUST keep).

**Recommended mechanism — deterministic post-grade filter (primary) + tier-aware prompt
(cooperative backstop):**

1. **Deterministic filter (the binding contract the test asserts).** Run the **full,
   unchanged** four-category grade. For the SIMPLE tier only, before the PASS/REVISE
   decision, **drop feedback entries whose cited JSON path is `expected_arrr.*`** (the prompt
   already mandates "cite the exact JSON path" — `self_evaluate_v1.md:99`). The drop is
   provably **one-directional**: it can only ever strip `expected_arrr`-path critiques, only
   on the simple tier — it can NEVER strip a `target_cohort` / `message_plan` /
   `selection_reason` / `legal` / `schema` / PII critique. If, after the drop, all categories
   are empty ⇒ PASS (SHIP). If anything remains ⇒ REVISE exactly as today.
2. **Tier-aware prompt (cooperative, reduces wasted REVISE round-trips).** Plumb a
   `grade_tier ∈ {simple, strict}` field through `SelfEvaluateAdapter.evaluate` →
   `SelfEvaluateInput` → the user payload, and add a conditional to `self_evaluate_v1.md`:
   on `grade_tier == "simple"`, do NOT flag `expected_arrr` defensibility (the basis-overstates
   and ARRR-vs-cohort sub-rules) — **every other rule, including all anti-fabrication, stays**.
   The filter in (1) is the safety net if the model still flags ARRR.

   Plumbing: extend the `SelfEvaluator` Protocol `evaluate(draft, criteria, *, tier=STRICT)`
   with a defaulted kwarg (back-compat: existing `FakeSelfEvaluator` and unit fixtures keep
   working). The gate passes the computed tier; the adapter forwards it as
   `context_summary`-adjacent input. `SelfEvaluateInput` gains
   `grade_tier: Literal["simple","strict"] = "strict"` (default strict = no behavior change
   for any current caller).

   *(Alternative considered and rejected: a second prompt file `self_evaluate_v1_simple.md`.
   Rejected — forks the four-category contract into two files that drift; the one-conditional
   approach keeps a single source.)*

**Config knob (Type-2 governed, like `max_revisions`).** `config/self_evaluate.yaml` gains a
`simple_tier:` block — `enabled: true`, `templates: ["team_winback_simple"]`. The cohort
ceiling is **imported from `L3_AUTO_MAX_BATCH`**, NOT duplicated in yaml (one source of truth;
see §4). `enabled: false` reverts to today's behavior (everything strict) — a one-line
kill-switch if the calibration misbehaves on dev.

### OFFER / LARGE tier — full grade (UNCHANGED)

The `else` branch is **byte-identical to today**: the full four-category grade, the
two-revise-then-reject policy (`max_revisions=2`), and SELF_EVAL_REJECTED → ESCALATE_TO_FAZAL.
No `expected_arrr` critiques are dropped. Reached by: `team_winback_offer` (money_bearing) at
**any** size, OR cohort_size > ceiling, OR any non-allow-listed / unresolvable template.

---

## 4. The cohort-size THRESHOLD + justification

**`SIMPLE_SHIP_MAX_COHORT = L3_AUTO_MAX_BATCH = 20`** (`agents/autonomy.py:35`) — imported,
not a fresh literal.

**Why 20, tied to an existing constant.** `L3_AUTO_MAX_BATCH = 20` is the **bulk
always-confirm floor**: `autonomy.is_always_confirm` returns `(True, "bulk")` when
`len(batch_customer_ids) > 20` (`autonomy.py:514`), which forces an L3-eligible batch BACK to
the **L2 owner checkpoint** at the send choke (`send_checkpoint.py:127`,
`l3_hold.enter_l3_hold`). So:

- A cohort **≤ 20** is exactly the regime where the send path itself considers the batch
  small enough to (potentially) run as a single autonomous batch. Waiving the ARRR
  *business-justification* here is consistent with the system's own definition of "small,
  low-risk send."
- A cohort **> 20** already trips the bulk floor and **owner-checkpoints regardless of tier**.
  Relaxing ARRR above 20 would be pointless (the send still stops for approval) and would
  signal "large no-defense campaign is fine" — which contradicts the principle. So above 20 ⇒
  strict ARRR grounding **and** an owner checkpoint. **One constant, two enforcement points,
  zero drift** — and if Fazal ever moves the bulk floor, the ARRR-relaxation ceiling moves
  with it automatically.

**Why not `DEFAULT_DETECTION_LIMIT = 50`.** That constant is the *detection* cap (how many
lapsed candidates are surfaced — `sales_recovery_executor.py:141`), not a *send-trust*
threshold. Using 50 would let a 21–50 cohort ship without ARRR while STILL bulk-checkpointing
at send — a confusing split with no principled basis. The ship-without-ARRR bar should be the
same line the autonomy model already draws for "small enough to be low-risk," which is 20.

---

## 5. Proof the strict path STILL REJECTS a fabricated large/offer campaign

The gate is **not** globally weakened — two structural facts prove it:

1. **The `else` branch is unchanged.** A fabricated large/offer campaign never enters the
   simple lane: `team_winback_offer` is `money_bearing: true` ⇒ strict; cohort_size > 20 ⇒
   strict; any other/unresolvable template ⇒ strict (fail-closed). On the strict path the full
   grade runs: `consistency` flags an implausible `expected_arrr.high` vs cohort, `pillar`
   flags overstated confidence / invented numbers, `schema` flags evidence-marker
   inconsistency, `legal` flags misleading financial claims — and two REVISE verdicts REJECT
   (SELF_EVAL_REJECTED → ESCALATE_TO_FAZAL, `sales_recovery.py:888`). PLUS money_bearing trips
   the always-confirm floor → owner checkpoint at send. Nothing on this path changed.
2. **Even the simple lane only relaxes ONE named axis.** On the simple tier, a fabricated
   **customer fact** (invented name, invented per-vertical number, retention-pressure
   language, PII in params, misleading financial claim) is still caught — only
   `expected_arrr`-path critiques are dropped. The deterministic filter is provably
   one-directional (it can only strip `expected_arrr.*` entries), so anti-fabrication cannot
   be bypassed by routing through the simple tier. And a simple plan that targets a bucket
   with zero real customers still fails the `consistency` **grounding** check (that sub-rule is
   NOT the ARRR sub-rule).

So the calibration **adds a narrower lane**; it never widens the existing one.

---

## 6. Test plan

**Unit — `tests/orchestrator/agent/test_self_evaluate_gate.py` (extend; existing cases must
stay green):**

- `test_simple_winback_small_cohort_weak_arrr_ships` — proposed plan,
  `template_id=team_winback_simple`, `cohort_size<=20`; `FakeSelfEvaluator` scripted REVISE
  carrying ONLY an `expected_arrr.*`-path critique ⇒ gate **SHIP**, status `passed`. Proves
  ARRR is non-blocking on simple.
- `test_simple_winback_fabricated_customer_fact_still_revises` — same tier; REVISE with a
  `pillar` invented-number critique citing `target_cohort.selection_reason` ⇒ gate **RETRY/
  REJECT** (not shipped). Proves anti-fabrication intact on simple.
- `test_simple_winback_ungrounded_cohort_still_revises` — same tier; REVISE with a
  `consistency` critique citing `target_cohort` vs distribution (NOT the ARRR sub-rule) ⇒
  **RETRY/REJECT**. Proves cohort-grounding survives the relaxation.
- `test_simple_template_large_cohort_falls_to_strict` — `team_winback_simple`,
  `cohort_size=21`; ARRR-only REVISE ⇒ **RETRY/REJECT**. Proves the threshold.
- `test_offer_template_uses_strict_grade` — `team_winback_offer` (money_bearing), any size;
  ARRR-only REVISE ⇒ **RETRY/REJECT**. Proves the offer path unchanged.
- `test_unresolvable_template_falls_to_strict` — bogus `template_id` ⇒ fail-closed strict;
  ARRR-only REVISE ⇒ **RETRY/REJECT**.
- Existing `test_proposed_plan_is_still_fully_graded`, `test_thin_proposed_plan_is_still_
  rejected`, the VT-491 short-circuit tests (`test_insufficient_data_short_circuits_*`,
  `test_out_of_scope_short_circuits_*`), and `test_evaluation_criteria_are_the_four_documented`
  — **must still pass unmodified** (tier logic runs AFTER the isinstance short-circuit and
  defaults strict for every current fixture).

**Adapter / prompt plumbing unit (`tests/.../tools/test_self_evaluate*`):** mock the Anthropic
client; assert the user payload carries `grade_tier="simple"` for a simple draft and
`"strict"` (or default) otherwise; assert the deterministic `expected_arrr.*` filter strips
only ARRR-path entries.

**Real-DB / integration (validate on dev, per CL-2026-06-29 — local LLM key is not a
blocker):** extend `tests/orchestrator/test_vt485_winback_grounding_realdb.py`:
- A genuinely-lapsed **small** cohort with a **thin L4 attribution baseline** (the VT-498
  failure mode) now produces a **gate-PASSED simple win-back** draft.
- The same cohort **upsized to 30** OR switched to **team_winback_offer** still **REVISEs/
  REJECTs** on ARRR.

**Safety-rails + anti-fabrication regression (both tiers):**
- Reuse the send-gate tests (`test_send_gate_optin_realdb.py`, the onboarded/opt-out gates) to
  assert consent / opt-out / onboarded fire **identically** for a simple-tier shipped plan —
  the calibration does not touch `customer_send` or the compliance gates.
- Assert a simple-tier "ship" does NOT bypass the send-checkpoint:
  `send_checkpoint_decision` still returns `CHECKPOINT` for an unproven (L2) tenant / novel
  template / first-contact, so a simple win-back still earns owner visibility at send.
- Assert the executor `validate_draft_params` literal-bundle grounding and the VT-498
  personalization scrub are unchanged (a revenue claim sneaked into params/personalization
  still fails).

---

## 7. Risk / edge cases

- **Simple template, huge cohort (>20)** → falls to STRICT (tier predicate) AND the send
  bulk-floor checkpoints. Double-covered.
- **Simple template that sneaks a revenue/offer claim into the message** → anti-fabrication
  catches it on BOTH tiers: self_evaluate `pillar`/`legal` (not relaxed) + the executor
  `validate_draft_params` (params must be literal bundle values — a free-text claim is not a
  bundle value) + VT-498 personalization scrub. The ARRR relaxation does not touch message
  content rules.
- **Registry drift / unresolvable `template_id`** → `_resolve_money_bearing` is fail-closed
  (`TemplateRegistryError → True` → strict). A typo'd or retired template can never reach the
  relaxed lane.
- **money_bearing flag accidentally true on team_winback_simple** → the `money_bearing is
  False` clause in the predicate forces strict (defence-in-depth beyond the name allow-list).
- **Non-proposed terminal (out_of_scope / insufficient_data)** → VT-491 isinstance
  short-circuit at `self_evaluate.py:289` fires FIRST and SHIPs unchanged; tier logic only
  runs for `CampaignPlanProposed`. No interaction.
- **`cohort_size` lying about scale** → impossible: the schema pins
  `cohort_size == len(customer_ids)` (`campaign_plan.py:182`), so the tier reads a truthful size.
- **Prompt-relaxation drift (Option A path)** → the deterministic `expected_arrr.*` filter is
  the binding backstop; even if the model ignores `grade_tier` and flags ARRR, the filter
  strips only ARRR-path entries on the simple tier. The filter — not the prompt — is what the
  tests assert.
- **Filter over-stripping** → bounded by construction: it matches the `expected_arrr.`/
  `expected_arrr ` path prefix only and only within the simple tier; it cannot remove a
  `target_cohort` / `message_plan` / `legal` / `schema` / PII critique. (A unit asserts a
  mixed-critique REVISE keeps the non-ARRR entries.)

---

## Build envelope (for the Cowork gate — not part of the design)

- Risk row (money + grade path): plan-first gate before build (this file). One coherent PR
  per VT-500.
- Files touched: `agent/self_evaluate.py` (tier classify + filter + Protocol kwarg),
  `agent/tools/self_evaluate.py` (forward `grade_tier`, `SelfEvaluateInput` field),
  `agent/tools/prompts/self_evaluate_v1.md` (one conditional), `config/self_evaluate.yaml`
  (`simple_tier` block + kill-switch), tests above. No schema change, no migration, no DB write.
- Adversarial-verify focus: (a) the strict `else` branch is byte-identical (diff proof);
  (b) the filter is one-directional (only `expected_arrr.*`, only simple); (c) every safety
  rail + anti-fabrication path is untouched.
- Validate on deployed dev (CL-2026-06-29), not locally.
