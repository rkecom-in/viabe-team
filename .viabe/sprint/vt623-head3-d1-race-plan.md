# D1 async-race root-cause + fix plan (task #46 / VT-623 Head3) — 2026-07-10

Root-caused end-to-end during the overnight run (CL-2026-07-10). The async `manager_task_workflow`
(enforce mode) decouples the owner reply from the triggering webhook turn, so both SR scenarios fail:
- `delegation_winback_plan`: [4.8,4.8,3.8] — 1 FAIL = step-1 TIMEOUT + plan at step-2.
- `sr_approved_send_completes_truthfully`: [1.8,2.4,3.6] — 0/3, "never confirms send after 'bhej do'".
Honesty CLEAN on both ⇒ the cohort/45d work is correct; this is the residual quality disease.

## Mechanism (files + lines, verified by reading)
1. `runner.py:1150-1156` triage_seam classifies `new_task` → `triage_seam.py:185-190` creates the plan
   + `start_manager_task_workflow` (DBOS fire-and-forget) → returns `skip_legacy_dispatch=True`.
2. `runner.py:1170` dispatch_brain SKIPPED; `final_status` stays "completed".
3. `runner.py:1229-1234` D1 check: completed + inbound + `_brain_emitted_owner_reply==False` (the async
   workflow hasn't replied yet) → `_send_completed_no_reply_fallback` fires the generic
   "Got it — I'm on it and I'll update you shortly."
4. The async `manager_task_workflow` (`workflow.py:834+`) then dispatches the specialist; the real
   plan/approval-ask SENDS inside `_dispatch_specialist_step`'s approval-arm — out-of-band, landing on
   the NEXT webhook turn (the harness's step-2), which the judge scores as a progression/intent lag.

## ONE root cause (a narrow-fix hypothesis was REFUTED during pre-verify — logged so nobody re-chases it)
### REFUTED: "successful send has no owner confirmation."
`_run_verification_cycle` (`workflow.py:624-628`) DOES notify on success: `verdict=="verified"` →
`_settle_verified_task` + **`_notify_owner_of_terminal`** (627). The success confirmation IS composed
(`owner_surface.task_outcome.maybe_notify_owner_of_task_outcome`). My first read saw only the TAIL
(1046 notifies on 'blocked') and missed the earlier success-notify. Building a tail success-notify
would have DOUBLE-sent "sent". → There is NO missing-notify bug; do not add a second notify.

### The real bug — async owner replies land OUT-OF-BAND from the triggering turn (BOTH scenarios).
The plan summary (delegation) and the send confirmation (`maybe_notify_owner_of_task_outcome`, approved
path) are both composed INSIDE the async `manager_task_workflow`, which the webhook turn started
fire-and-forget. The sync turn returns with `skip_legacy_dispatch=True` and no reply yet → the D1
"I'm on it" fallback fires (or, for the approval turn, the paused_approval poll + notify complete after
the turn). So the substance arrives a turn late; the in-turn reply is a non-answer. NOT a missing reply
— a TIMING/decoupling issue.

## Fix direction (recommended: bounded in-turn wait — narrow + verifiable)
- **B1 (RECOMMEND): a bounded in-turn wait in `runner.py` before the D1 fallback.** After
  `start_manager_task_workflow` (skip_legacy_dispatch True), poll `_brain_emitted_owner_reply` for up
  to a TIGHT budget (~a few seconds); if the async task emits its real reply in time, send NOTHING (the
  real reply already went); only if it doesn't, send "I'm on it". Fast tasks (plan summary, send
  confirm) then reply IN-TURN; only genuinely-slow tasks get the ack. Narrow (one bounded poll), directly
  fixes both scenarios. RISK: adds latency to every new_task turn — bound it hard; measure.
- **B2 (defer to Fazal design call): synchronous fast-path** — run a simple new_task (plan summary)
  synchronously in the webhook turn. Larger enforce-loop change.
- Also consider: the approval-turn ("bhej do") is `answer_pending` → skip_legacy_dispatch True → the
  send confirm lands via the async paused_approval poll; the same bounded in-turn wait covers it.

## Build discipline (005500Z: enforce loop = MAXIMUM self-verification)
1. full-pack x3 BASELINE first (method: run_full_pack x3 via measure harness — decide whole-pack vs
   critical-subset; note which). 2. Build Bug-A (+ B1 if clean). 3. adversarial-verify the enforce-loop
   edit (double-send + replay-idempotency the hardest lens). 4. full-pack x3 AFTER; confirm no regression
   + the two SR scenarios lift. 5. self-merge dev on green. One coherent PR.

## Status: Phase-1 root-cause DONE (this doc). Build deferred from the tail of the 2026-07-10 overnight
session (the 45d PR consumed it) so the enforce-loop change gets its own careful verified pass, not a
rushed change. NO real send — validate via the synthetic harness only.
