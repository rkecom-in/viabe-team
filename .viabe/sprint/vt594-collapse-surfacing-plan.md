# VT-594 plan — delegated work must surface to the owner (collapse-path reply seam)

**Risk row** (touches campaign/approval surface) → this plan file = the self-run plan-first gate (CC full autonomy, CL-2026-06-28).

## Root cause (proven by local repro, 2026-07-04)

1. **The crash (observability only):** `compose_owner_output` assumes an `AgentResult`-shaped
   `specialist_result` with `.output`. The collapse path passes the raw `CampaignPlan` pydantic
   model. `output_composer.py:473` (`_derive_template_params`), `:509` (`_derive_free_form_body`),
   `:528` (`_derive_follow_up`) do bare `specialist_result.output` →
   `AttributeError: 'CampaignPlanProposed' object has no attribute 'output'` (repro'd locally).
   Swallowed at dispatch.py:1123 → empty envelope. This only degrades observability.

2. **The real defect (the D1 symptom):** NOTHING sends an owner message on any COMPLETED collapse
   terminal. `_maybe_send_manager_reply` (VT-589) is gated `terminal_path=="terminal"` on the wrong
   assumption that collapse "transmits its own owner-facing message elsewhere". Only the
   PAUSED path (proposed → approval gate) sends anything (the approval template inside
   `arm_pause_request`). Every completed collapse run tells the owner nothing → runner.py D1
   fallback "Got it — I'm on it".

## The six silent collapse cases (all end completed + collapse)

| # | Terminal state | Today | Correct owner surface |
|---|---|---|---|
| 1 | `campaign_rejected` (`_CohortRejectedResult`) | silence → D1 | honest count-only "couldn't verify N targets; nothing sent" (VT-241 privacy: count only) |
| 2 | `CampaignPlanOutOfScope` | silence → D1 | honest "outside what I can do here: {out_of_scope_reason}" |
| 3 | `CampaignPlanInsufficientData` | silence → D1 | honest "not enough data yet: {missing_data summary}" + next step |
| 4 | Proposed + `owner_decision=="queue_busy"` (VT-369 serialized queue) | silence → D1 | "plan saved; you already have one approval waiting — answer that first" |
| 5 | Proposed + `owner_decision=="send_failed"` | silence → D1 | "plan drafted + saved; couldn't deliver the approval prompt; it'll come at the next sync" |
| 6 | Proposed + NO owner_decision (VT-334 weekly-budget skip returned `{}`) | silence → D1 | "plan drafted + saved; holding the formal approval ask (several already this week) — it comes with the weekly sync" |

Paused path (proposed → gate interrupt) returns early at `__interrupt__` → none of this fires → no double-send.
Resume path (`approval_resume.resume_run`) invokes the graph directly, NOT via `dispatch_brain` → unaffected.
`dispatch_brain` is called ONLY from runner.py webhook path → every run here is owner-inbound-triggered.

## Changes

**A. `output_composer.py` — crash fix (mechanical):** the three bare `.output` reads become
`getattr(specialist_result, "output", None)`. Composer never raises on a CampaignPlan again.

**B. `agent/dispatch.py` — `_maybe_send_collapse_reply(tenant_id, event, terminal_state, specialist_result)`:**
mirror of `_maybe_send_manager_reply` (never raises, exactly one send). Called when
`terminal_path=="collapse" and final_status=="completed"` (same `event.message_type=="inbound_message"`
gate as D1 for symmetry). Dispatch by the table above — deterministic, substance-railed bodies built
ONLY from the plan's own typed fields (never fabricated specifics; counts/segment labels, no customer
ids — VT-241/CL-390). en/hi register via `resolve_owner_locale` (detail strings stay as the agent wrote
them). Send via `send_freeform_ack` → records the assistant turn → `_brain_emitted_owner_reply` True →
D1 auto-suppressed. Fail-soft: any error → log + let D1 remain the net.

**C. In-chat plan SUMMARY before the approval prompt (the has-plan case):** in `collapse_node`'s
proposed branch, after `collapse_campaign_plan` persists and BEFORE the gate routing, best-effort
`send_freeform_ack` of a deterministic plan summary (cohort size + segment label + window + expected
recovery range + one-line selection reason) — so the owner sees WHAT they're approving before the
approval template arrives. Gated on `trigger_reason == "owner_initiated"` (a weekly-cadence proposal
keeps today's prompt-only behavior; no new unsolicited sends). try/except-wrapped: a summary-send
failure must never unwind the persist or block the gate.

**Not in scope:** resume-path surfaces (approved/rejected/timeout notifications), the gate's template
itself, cadence-triggered summaries, richer LLM-composed summaries (deterministic first — the fields
are typed; zero extra LLM cost).

## Tests (TDD — failing first)

- `test_output_composer.py`: compose with each CampaignPlan variant + `_CohortRejectedResult` → no
  raise; free-form body derivation sane.
- dispatch tests: for each of the 6 cases → exactly one `send_freeform_ack` with the expected body
  class; terminal path unchanged; paused path sends nothing new from dispatch; `terminal_path=="terminal"`
  behavior untouched (VT-589 regression).
- collapse tests: proposed+owner_initiated → summary send called before gate attach; cadence trigger →
  no summary; summary-send raise → collapse still returns pending_approval_request; budget-skip path
  unchanged (returns `{}`).
- Existing suites stay green (`test_collapse.py`, `test_dispatch*`, VT-589/590/591 tests).

## Validation on deployed dev (after land+push)

Seed `--onboarded --seed-lapsed-customers 8` tenant → drive "make me a plan to win back my lapsed
customers" → read the FULL reply (sed range, NEVER first-line grep — the 6-deploy-phantom lesson).
Expect: a real plan summary + approval prompt (if cohort non-empty) or the honest insufficient-data
reply (if VT-596 consent gate zeroes the cohort — that outcome feeds VT-596 #2 either way). Confirm no
`compose_owner_output raised` in logs; confirm no D1 line.
