# VT-632 overnight report + Step-5 plan (2026-07-09, CC autonomous)

## TL;DR
- **Root cause of the dominant "I'm on it" breaker: CONFIRMED + it was NOT what the design/I first assumed.**
  Dev is `TEAM_MANAGER_LOOP_MODE=enforce` (I had it wrong as "legacy"). Triage classifies an answerable
  READ ("cash flow — only the number") as `new_task` → the async manager_task path owns the turn →
  `dispatch_brain` is skipped → the sync webhook D1-stalls "Got it — I'm on it." The brain NEVER ran on the
  failing turn (cost=0, steps=0). 4× DB-verified via `--keep-tenants` traces.
- **Step 1 SHIPPED + deployed + confirmed** (commit 16b572c): `manager_triage.md` reclassifies answerable
  READs → `direct_reply` (→ sync brain answers in-turn). efficient_no_overstep turn-0: **FAIL→PASS**,
  deterministic. Triage stable, no over-shift (win-back/connect/draft stay `new_task` 3/3).
- **The measurement surfaced the REAL top blocker: the gate is variance-dominated.** A single-run 12-cohort
  judge looked like widespread regression — but it's NOISE (proof below). You cannot measure a manager fix
  with a single run.

## What I did NOT do, and why (adequate call)
Did not ship Step 5 / answer-quality fixes overnight. They are money-adjacent (Cowork: plan-first +
adversarial-verify), need a trace-first root cause, and are un-measurable tonight under the variance +
a dev degraded by a killed measurement run. Shipping fix-after-fix against a noisy gate is churn. Step 1
(safe, deterministic) is landed; the rest is teed up below.

## The variance finding (this reframes the >95% effort)
Post-Step-1 single-run judge vs single-run baseline showed "regressions": delegation_empty_cohort 4.6→1.4,
bilingual_hinglish 4.2→2.0, delegation_analytical 3.8→2.4, etc. **NOT regressions:**
- `delegation_empty_cohort` ("make me a plan to win back") STAYS `new_task` (triage 3/3) — Step 1 provably
  does not touch its path. A 3.2-pt swing on an UNCHANGED path = pure run-to-run variance.
- The first measurement was killed mid-run → orphaned DBOS workflows → degraded dev for the second run I judged.
- Conclusion: **single-run judge tables are noise (±3 pts). Must run x3+ per scenario (run_critical_x3) and
  compare distributions.** (Matches the VT-611 "variance = top blocker" note — now proven to invalidate
  single-run gate comparisons.)
- The variance is INFRA-driven, not LLM-sampling: the async win-back path is INTERMITTENT — it scores 4.6
  when the async task notifies, 1.4 when it D1-stalls.

## Remaining diseases (prioritize with a TRUSTWORTHY x3 baseline first)
1. **Async win-back D1-stall (VT-632 Step 5)** — legit `new_task` win-backs intermittently stall "I'm on it"
   because the async terminal doesn't guarantee an owner reply. This is the async VARIANCE-REDUCER.
2. **Brain answer-QUALITY on reads (Tier-2 / decision-quality)** — when the brain answers a read it sometimes
   answers the WRONG read (cash-flow ask → campaign fact) or a false "no data" (analytical, contradicted the
   next turn). Separate lever from routing.
3. **Onboarding stuck-loop** (bilingual_hinglish: 3× identical confirm, ignored "aage badho") — a VT-616-class
   loop in the onboarding_conductor lane.
4. **Infra variance-drivers**: async `manager_dispatch` escalating cost-0 "terminated without spawning";
   `tenant_alerts_trigger_kind_check` rejecting 'silent_terminal' (fail-soft CheckViolation, real traceback);
   orphaned workflows.

## Step 5 plan (plan-first; build next with adversarial-verify)
**Goal:** every async manager_task terminal guarantees ONE honest owner reply — no async turn ends in silence
or the generic D1 "I'm on it." Makes the win-back path reliable (kills the intermittent stall).

- **STEP 0 partial finding (2026-07-09):** a `--keep-tenants` delegation_empty_cohort run **PASSED** —
  "I don't have enough customer data yet…" (honest InsufficientData) delivered IN-TURN via `route: none`
  (SYNC dispatch_brain → spawn_sales_recovery → collapse → `_maybe_send_collapse_reply`). So the win-back
  cluster **intermittently routes SYNC (collapse → honest answer = PASS) vs ASYNC (new_task → D1-stall = FAIL)**.
  The enforce `triage_seam` falls through to SYNC when the plan isn't admitted 'planned' (validation fail /
  'queued'), and takes ASYNC when it is. **The SYNC collapse path ALREADY produces the correct honest empty-
  cohort answer; the ASYNC terminal does NOT surface it.** So Step 5 = port that honest surfacing onto the
  async terminal (or converge the two paths). STILL NEEDED: catch a run that takes the ASYNC branch
  (`--keep-tenants`, retry until it D1-stalls) and DB-trace whether its async terminal is a clean
  insufficient-data terminal (notify-branch fix) or a cost-0 escalate (deeper). Do NOT build before that.
- **Fix surface (if mode (a)):** `owner_surface/task_outcome.py::maybe_notify_owner_of_task_outcome` handles
  only `{completed_with_effect, completed_no_action, cancelled}`. `failed`/`escalated` land the task at
  non-terminal `blocked` + a VTR incident with `owner_notification_status` never `pending` → owner gets
  nothing. Add an honest escalate/blocked/empty-cohort owner notify (idempotent, DBOS-orphan-safe, Pillar-7
  honest — never claim success). `manager/workflow.py` sets the outcome + `owner_notification_status='pending'`.
- **Guardrails:** money-adjacent → adversarial-verify (the notify must never fabricate a send/success/number);
  exactly-once (owner_notification_status gate); dev-only; measure with x3 (delegation_empty_cohort +
  sr_empty + the winback follow-ups reliably reach the owner truthfully).

## Ops notes
- `.md` prompt files are FUNCTIONAL code but `paths-ignore`'d by `deploy-dev.yml` → prompt-only changes ship
  with NO CI validation; Railway's native git auto-deploy still deploys them. Fine, but no CI safety net on prompts.
- Never kill an enforce harness mid-run (orphans DBOS workflows → degrades subsequent runs).
