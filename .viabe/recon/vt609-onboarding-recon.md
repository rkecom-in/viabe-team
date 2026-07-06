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

## The 85-behavior mapping table (team-lead ruling deliverable, 2026-07-06)

Sort of every named test in the 5 legacy files (24+9+24+11+17=85) into the ruling's three
buckets. Buckets: **SAFETY-TOOL** = the guard is enforced inside a tool/pure-decision-library the
specialist cannot bypass, proven by a tool-boundary test (zero LLM mocking). **STRUCTURAL** =
immune by construction (no parallel mechanism exists to fail). **LEGACY-ONLY** = mode-gated
mechanism specific to the walker/turn_brain internals with no enforce-mode analog needed (the
thing it protects against cannot occur in the new design). **QUALITY→VT-611** = model-judgment/
pacing behavior, behavioral proof deferred to the real-scenario pack. **GAP-CLOSED** = a real gap
this audit found, fixed on this branch, with a new test. **GAP-FLAGGED** = an open design question
raised to team-lead, not fixed here.

Per the ruling: no never-assert / opt-out-wins / bare-rejection / never-downgrade / skip-answered-
no-reask guard sits in QUALITY→VT-611. Verified below — all five families land in SAFETY-TOOL or
STRUCTURAL.

### test_journey.py (24)

| # | Test | Bucket | Proof / reason |
|---|---|---|---|
| 1 | test_start_journey_and_get_journey_basics | STRUCTURAL | `start_journey`/`get_journey` untouched shared substrate; still called by `signup.py` (journey creation) and wrapped by `read_onboarding_state`. |
| 2 | test_start_journey_replaces_existing | STRUCTURAL | same substrate, unaffected. |
| 3 | test_set_queue_if_empty_fills_only_when_empty | LEGACY-ONLY | cursor/`question_queue` walker mechanism; the specialist has no queue (recomputes fresh every call). Mode-gated, byte-identical — `test_runner_onboarding_mode_gate.py`. |
| 4 | test_set_queue_if_empty_noop_on_complete | LEGACY-ONLY | same. |
| 5 | test_handle_reply_confirm_promotes_to_canonical_profile | SAFETY-TOOL | `confirm_field_answer` → `_confirm`/`confirm_draft`. Proof: `test_journey_specialist_tools.py::test_confirm_field_answer_promotes_valid_business_type`. |
| 6 | test_handle_reply_confirm_correction_is_the_value | SAFETY-TOOL | VT-477 the-value invariant: `_is_bare_rejection_value` refuses a bare affirmation AS a value — the caller must pass the actual value. Proof: `test_journey_specialist_tools.py::test_confirm_field_answer_rejects_bare_affirmation_value`. |
| 7 | test_handle_reply_gap_stored_in_answers | SAFETY-TOOL | `record_extracted_answer`. Proof: `test_record_extracted_answer_records_without_promoting`. |
| 8 | test_handle_reply_skip_adds_to_skipped | SAFETY-TOOL | `record_field_skip`. Proof: `test_record_field_skip_defers_field`. |
| 9 | test_handle_reply_bare_greeting_mid_confirm_not_recorded | SAFETY-TOOL | `confirm_field_answer`'s bare-rejection guard. Proof: `test_confirm_field_answer_rejects_bare_greeting_value`. |
| 10 | test_handle_reply_bare_greeting_mid_gap_not_recorded | SAFETY-TOOL | `record_extracted_answer`'s bare-rejection guard. Proof: `test_record_extracted_answer_rejects_bare_greeting_value`. |
| 11 | test_handle_reply_bare_no_to_confirm_not_recorded_as_value | SAFETY-TOOL | same guard family (negative). Proof: `test_confirm_field_answer_rejects_bare_negative_value` / `test_record_extracted_answer_rejects_bare_negative_value`. |
| 12 | test_handle_reply_greeting_mixed_with_answer_is_recorded | SAFETY-TOOL | proves the guard is a set-⊆ check, not substring — a value with real substance mixed with greeting words is NOT over-rejected. Proof: `test_record_extracted_answer_records_greeting_mixed_with_substance`. |
| 13 | test_handle_reply_idempotent_redelivery_no_double_advance | STRUCTURAL | redelivery dedup happens upstream at the runner (`twilio_inbound_events` UNIQUE + `dupe_status`, checked before the gate at `runner.py:890` in ALL modes) — a duplicate inbound never reaches the specialist. Independently, the write tools have no cursor to double-advance (same-value writes are naturally idempotent). |
| 14 | test_handle_reply_completion_fires_gap4_seam | SAFETY-TOOL | `_maybe_complete_from_specialist` → `_complete` → `_emit_gap4_seam`. Proof: `test_confirm_field_answer_completes_profile_and_fires_gap4_seam`, `test_record_field_skip_can_itself_complete_the_profile`. |
| 15 | test_handle_reply_on_complete_journey_returns_done | STRUCTURAL | reuses unchanged `conductor.profile_collection_complete`/`next_question_for_tenant` (pre-VT-609 pure functions). Proof: `test_conductor.py::test_completion_is_deterministic_not_self_declared`, `test_next_question_none_signals_but_does_not_self_complete`. |
| 16 | test_recompose_heals_stale_category_confirm_preserving_progress | STRUCTURAL | VT-478 healed a frozen-queue staleness class. The specialist has NO frozen queue — `next_required_question` recomputes fresh every call, so staleness cannot exist. |
| 17 | test_recompose_leaves_a_non_stale_queue_untouched | STRUCTURAL | same — moot, no queue to touch. |
| 18 | test_recompose_via_intercept_auto_heals_then_confirm_advances | LEGACY-ONLY + STRUCTURAL | legacy/shadow-only mechanism (mode-gated); obsolete-by-construction in enforce. |
| 19 | test_vt477_confirm_yes_records_draft_value_and_advances_exactly_once | SAFETY-TOOL + STRUCTURAL | bare-affirmation-value guard (row 6) + no advance-counter to double-fire (no cursor). |
| 20 | test_vt477_five_greetings_then_yes_advances_once | SAFETY-TOOL + STRUCTURAL | bare-greeting guard (row 9/10) + no-cursor. |
| 21 | test_vt477_redelivered_yes_does_not_double_advance | STRUCTURAL | redelivery dedup upstream (row 13) + no cursor. |
| 22 | test_vt601_descriptive_type_correction_cross_fills_about_no_reask | QUALITY→VT-611 | cross-field inference from a correction is model judgment (deciding a related field is now also answered) — extraction-quality, VT-601. |
| 23 | test_vt601_bare_yes_confirm_does_not_cross_fill | SAFETY-TOOL (negative half) + QUALITY→VT-611 (cross-fill half) | the "yes never becomes the value" half is the bare-rejection guard (row 6); the "does not cross-fill" half has nothing to over-fire deterministically (cross-fill is the model's own inference, never a tool side-effect). |
| 24 | test_vt601_already_answered_field_entry_never_re_presents | SAFETY-TOOL | skip-answered-no-reask — `decide_next_question` drops `answered` fields at the candidate source (`conductor.py:123-124`), unchanged, thinly wrapped by `next_required_question`. Proof: `test_conductor.py::test_volunteered_out_of_order_field_not_reasked` (pre-existing, exercises the exact function the tool wraps). |

### test_journey_intercept.py (9) — the OLD gate's own mechanism; all mode-gated LEGACY-ONLY unless noted

| # | Test | Bucket | Proof / reason |
|---|---|---|---|
| 1 | test_no_journey_trial_tenant_falls_through | LEGACY-ONLY | gate null-check; in enforce the gate doesn't run at all — Manager routing (VT-465/603) owns this, out of the 85's scope. |
| 2 | test_pending_journey_fills_queue_from_draft | LEGACY-ONLY | queue-fill mechanism, no queue in specialist. |
| 3 | test_active_journey_delegates_to_handle_reply | LEGACY-ONLY | proof the gate delegates; enforce analog = Manager routes the turn to the specialist node — proof: `test_runner_onboarding_mode_gate.py`. |
| 4 | test_redelivered_inbound_does_not_resend_pending_question | STRUCTURAL | dupe_status upstream (row 13 above) — the gate/specialist never re-fires for a duplicate in either mode. |
| 5 | test_bare_greeting_mid_journey_re_presents_without_advancing | LEGACY-ONLY (mechanism) + SAFETY-TOOL (guard, dupe-safety, rows 9/10) |
| 6 | test_established_tenant_no_journey_returns_none | LEGACY-ONLY | gate null-check, moot in enforce (gate never intercepts). |
| 7 | test_fail_open_on_internal_error_returns_none | SAFETY-TOOL | direct analog is the deterministic floor — a specialist failure never crashes/silences, it composes a scripted reply instead (stronger guarantee than fall-through). Proof: `test_onboarding_conductor_floor.py::test_floor_composes_scripted_next_question_on_invoke_failure`, `test_floor_never_silences_with_no_tenant_id`. |
| 8 | test_complete_journey_drives_flow_then_falls_through | **GAP-FLAGGED (item A)** | drives the VT-576 paced post-profile flow (readiness-ask → integration-offer → plan-kickoff). No enforce-mode equivalent exists yet — open design question already raised to team-lead (who builds the post-profile bridge). Not silently dropped into QUALITY. |
| 9 | test_optout_during_active_journey_falls_through_not_consumed | STRUCTURAL | opt-out-wins — in enforce the gate is mode-gated OUT entirely, so an opt-out inbound reaches `pre_filter` (the authoritative handler) directly with nothing intercepting first. Proof: `test_runner_onboarding_mode_gate.py` (enforce never calls `maybe_handle_journey_reply`) + `pre_filter`'s own unchanged opt-out tests. |

### test_journey_paced_flow.py (24) — ALL gap-flagged item A

Every test in this file exercises `_maybe_handle_post_profile_flow`/`_flow_*` (VT-576/VT-583 paced
post-profile flow: readiness-ask, integration-offer, one-beat-per-turn, plan-kickoff-after-data,
shop-domain detection, conversation-log threading). **Bucket: GAP-FLAGGED (item A)** for all 24 —
this is the SAME open design question as intercept-test #8 above, not 24 separate gaps: the paced
flow has no enforce-mode equivalent yet. Two are pure helper functions independent of any gate
(`test_recent_shop_domain_*`, `test_recent_owner_texts_helper_is_owner_only_newest_first`) and stay
green regardless of mode (STRUCTURAL, unaffected) — flagged here only because whoever builds item A
will reuse them. Full list: test_readiness_ambiguous_classified_decline_defers,
test_readiness_ambiguous_classified_affirm_offers, test_readiness_ambiguous_classifier_failure_keeps_today_behavior,
test_readiness_floor_still_short_circuits_without_classifier, test_deferred_ambiguous_connect_reengages,
test_deferred_ambiguous_other_falls_through, test_integration_orphan_reoffers_instead_of_silent_brain,
test_integration_live_resume_defers_to_gate, test_ack_after_card_asks_readiness,
test_yes_offers_single_best_integration_with_instructions, test_only_one_message_per_beat_no_burst,
test_defer_offers_honest_summary_only_and_is_resumable, test_deferred_unrelated_message_falls_through,
test_plan_fires_only_after_data_lands, test_plan_does_not_fire_before_data_lands,
test_plan_kicked_terminal_falls_through, test_redelivered_flow_message_does_not_redrive,
test_recent_shop_domain_found_and_normalized (STRUCTURAL/helper), test_recent_shop_domain_ignores_bot_lines_and_absence (STRUCTURAL/helper),
test_recent_shop_domain_from_unified_log_when_journey_dropped_it (STRUCTURAL/helper), test_recent_owner_texts_helper_is_owner_only_newest_first (STRUCTURAL/helper),
test_flow_beat_reply_recorded_to_conversation_log, test_walker_send_threads_tenant_id_to_conversation_log,
test_recent_shop_domain_reads_current_body_first (STRUCTURAL/helper).

### test_journey_populate_first.py (11)

| # | Test | Bucket | Proof / reason |
|---|---|---|---|
| 1 | test_populate_promotes_validated_derivable_and_suppresses_raw_category | STRUCTURAL | `populate_profile_from_draft` unchanged, shared substrate — now ALSO consumed by the specialist (`read_onboarding_state`). Proof of dual-consumption: `test_read_onboarding_state_runs_populate_first_and_surfaces_delta`. |
| 2 | test_populate_offtaxonomy_type_is_not_asserted_but_category_is | SAFETY-TOOL | never-assert, function-level guard, unchanged, reachable via `read_onboarding_state`. |
| 3 | test_populate_is_idempotent_no_recard | STRUCTURAL | function-level, unchanged, shared. |
| 4 | test_populate_owner_stated_value_is_never_downgraded | SAFETY-TOOL | never-downgrade — function-level guard, unchanged, reachable via `read_onboarding_state`. |
| 5 | test_populate_refresh_updates_changed_field | STRUCTURAL | shared substrate mechanism. |
| 6 | test_populate_requires_identity_anchor | SAFETY-TOOL | guard against populating from an unanchored draft — function-level, unchanged. |
| 7 | test_populate_anchors_on_owner_linked_website | STRUCTURAL/SAFETY-TOOL | same family, function-level, unchanged. |
| 8 | test_midflight_catchup_presents_card_and_suppresses_double_ask | split: SAFETY-TOOL (suppress-double-ask half) + QUALITY→VT-611 (card-presentation half) | suppress-double-ask is structural (a populated field lands in `answers`, so `decide_next_question`'s answered-exclusion, row 24 above, drops it automatically); HOW the card is presented to the owner is the specialist's own composition (prompt v2 instructs it), model-authored UX. |
| 9 | test_empty_necessities_completes_after_card | **GAP-CLOSED** (this session) | populate-only completion never transitioned journey status (`populate_profile_from_draft` itself never calls a completion check — the legacy walker only completes inline at its OWN lazy-start call site; the specialist calls populate on EVERY turn with no such call site). Fixed: `journey.maybe_complete_from_populate` + wired into `read_onboarding_state`. Proof: `test_read_onboarding_state_populate_delta_triggers_completion_recheck` (+ fail-soft: `test_read_onboarding_state_completion_recheck_failure_is_fail_soft`, no-op-skip: `test_read_onboarding_state_empty_populate_delta_skips_completion_recheck`). Commit `1fcedcf`. |
| 10 | test_edit_after_populate_repromotes_to_canonical | SAFETY-TOOL | owner-edits-forever — reuses `confirm_draft`'s merge-upsert via `confirm_field_answer`/`apply_correction`. Proof: `test_confirm_field_answer_correction_overwrites_prior_value`. |
| 11 | test_build_prompts_renders_card_and_strips_sentinel | SAFETY-TOOL (strip guard) + LEGACY-ONLY (card-rendering mechanism) | `turn_brain._visible_answers` strips ALL `__`-prefixed sentinels (`__flow__`, `__populated__`) generically — shared, unchanged, and `read_onboarding_state` calls the SAME function. Proof: `test_onboarding_conductor_write_tools_tenant_scope.py::test_read_onboarding_state_business_name_from_model_uses_context_tenant` asserts the strip. The walker's own card-RENDERING (`_fmt_profile_card`) stays legacy-only. |

### test_journey_turn_brain.py (17) — turn_brain is the LEGACY walker's own LLM-assist layer; the specialist's own reasoning loop replaces it entirely in enforce

| # | Test | Bucket | Proof / reason |
|---|---|---|---|
| 1 | test_no_to_confirm_records_nothing_and_reply_is_non_identical | LEGACY-ONLY (mechanism) + SAFETY-TOOL (guard, dupe-safety row 9/10/11) |
| 2 | test_multi_field_extraction_records_and_promotes_valid_confirm | QUALITY→VT-611 | multi-field-per-turn extraction is model judgment; the promotion GATE itself is proven separately (row 5 in test_journey.py table). |
| 3 | test_offtaxonomy_business_type_is_recorded_but_never_promoted | SAFETY-TOOL | never-assert, dupe-safety — proof: `test_confirm_field_answer_never_asserts_off_taxonomy_business_type`. |
| 4 | test_turn_brain_failure_falls_back_to_walker_bare_no | SAFETY-TOOL | direct analog is the deterministic floor. Proof: `test_floor_composes_scripted_next_question_on_invoke_failure`. |
| 5 | test_gate_off_is_deterministic_walker | LEGACY-ONLY | the OLD turn_brain feature flag (separate from `TEAM_MANAGER_LOOP_MODE`); mode-gated regardless. |
| 6 | test_completion_uses_durable_closer_and_does_not_burst_seam | STRUCTURAL | `_maybe_complete_from_specialist` only calls `_complete` when `status=='active'`; `_complete`'s own UPDATE is `WHERE status='active'` — a second call is a structural no-op, cannot double-fire the seam. |
| 7 | test_idempotent_redelivery_does_not_reinvoke_llm | STRUCTURAL | redelivery dedup upstream (dupe_status) — the specialist's LLM is never invoked at all for a duplicate inbound. |
| 8 | test_reprompt_after_no_is_non_identical | QUALITY→VT-611 | reply-wording variety is model-composition UX, not a tool-enforceable property. |
| 9 | test_confirm_button_set_detection_and_inline_cap | QUALITY→VT-611 | WhatsApp button-reply interpretation is the model's own multi-modal reasoning. |
| 10 | test_build_prompts_includes_recent_conversation | LEGACY-ONLY | turn_brain's own prompt-building mechanism; the specialist gets conversation history via the Manager's dispatch context (separate substrate), out of VT-609 scope. |
| 11 | test_append_recent_turns_caps_and_preserves_order | LEGACY-ONLY | VT-569 memory-management mechanism, turn_brain-internal. |
| 12 | test_overflow_fires_distill_with_evicted_head_and_prior_summary | LEGACY-ONLY | VT-571 distillation, turn_brain-internal. |
| 13 | test_no_overflow_does_not_fire_distill | LEGACY-ONLY | same. |
| 14 | test_distill_unavailable_dbos_does_not_break_append | LEGACY-ONLY | same, fail-soft mechanism. |
| 15 | test_distill_workflow_body_updates_summary_and_get_journey_exposes | LEGACY-ONLY | same. |
| 16 | test_distill_workflow_body_none_leaves_prior_summary_unchanged | LEGACY-ONLY | same. |
| 17 | test_mig163_conversation_summary_column_present_and_idempotent | STRUCTURAL | migration-presence check, mode-agnostic, unaffected either way. |

### Tally

SAFETY-TOOL (incl. dupe-safety cross-refs): 30-ish behaviors across the 5 named guard families
(never-assert, bare-rejection ×3 variants, never-downgrade, skip-answered-no-reask, opt-out-wins via
STRUCTURAL, completion-transition, LLM-down floor) — every one proven at a tool/pure-function
boundary with zero LLM mocking. STRUCTURAL: ~15 (no-cursor idempotency, dupe_status upstream,
obsolete-by-construction VT-478 staleness, shared unchanged substrate). LEGACY-ONLY: ~26 (turn_brain
internals + VT-478 intercept mechanism + queue mechanism) — mode-gated, byte-identical, no enforce
analog needed because the thing they protect against (frozen queue, turn_brain's own memory
management) cannot exist in the new design. QUALITY→VT-611: ~6 (VT-601 cross-fill, multi-field
extraction, reply-wording variety, button-click parsing, midflight-card presentation) — pacing/
extraction-quality only, per the ruling's own bucket-3 definition. GAP-CLOSED this branch: 4 total
(phase 2: missing completion-transition wiring for the 3 write tools + bare-affirmation-value guard
+ populate-first wiring into `read_onboarding_state`; this session: populate-only completion never
transitioned journey status). GAP-FLAGGED: item A (post-profile paced flow bridge) — 24+1 tests
converge on this single open design question, not 25 separate gaps.

