# VT-609 recon (2026-07-05, sonnet — the conversion contract)

## Conversion map (old seam → tool)
get_journey (journey.py:200) → read_onboarding_state
_apply_turn_plan extraction half (1046-1050) → extract_owner_answer
_confirm/confirm_draft (501-509 + draft_profile.py:66) → record_answer (the CL-390 never-assert promotion gate)
conductor.next_question_for_tenant (conductor.py:177) → next_required_question (thin wrapper)
handle_reply skip path (434-436) → record_skip
_reprompt_after_no (361-382) + turn-plan mark_rejected → apply_correction
conductor.profile_collection_complete (conductor.py:138) → profile_completion_check (thin wrapper)
onboarding_gate.is_agent_eligible → activation_check
NEW policy-confirmation tool → business_policy.grant_business_policy (337-387 — BUILT, ZERO callers today; deny-all default stands until this wires)
runner.py:873-894 interceptor → REMOVED in enforce (Manager routes to onboarding_conductor)
turn_brain None→handle_reply fallback (1219-1222) → the deterministic floor RELOCATES into the specialist's tool-call failure path (VT-597 shape: positive off-script signal falls through; classifier-None keeps the floor)

## The A2 regression contract: 85 journey tests (by file: test_journey.py 24, _intercept 9,
## _paced_flow 24, _populate_first 11, _turn_brain 17) + conductor/agent tests (7+5+6+9+2).
## Named must-survive behaviors + exact test names: in the recon message (session history
## 2026-07-05) — idempotent redelivery ×5, bare-greeting ×4, VT-569a ×3, VT-478 healing ×3,
## VT-477 ×2, VT-601 ×3, VT-576 pacing ×6, VT-583 intent ×6, run-23 orphan ×3, populate-first ×4,
## turn-brain fail-soft ×2, never-assert taxonomy ×1, opt-out-wins ×1, fail-open ×1, conductor
## invariants ×3 (test_conductor_holds_no_send_or_write_tool MUST be deliberately updated —
## write-tools are the point of the conversion; guardrail is name-substring based and permits
## journey-write tools structurally).

## Notes
- VT-603 already fixed tenant scoping on both existing conductor tools.
- Gate #3 (Shopify resume, runner.py:896-919) couples via _integration_resume_live
  (journey.py:1352-1364) — VT-608 territory; VT-609 must not break the coupling while VT-608's
  enforce-defer ruling (see vt608 recon rulings) handles gate ownership.
- Drift note from recon: correct — VT-607 was mid-build at recon time (FK sub-task committed only).
