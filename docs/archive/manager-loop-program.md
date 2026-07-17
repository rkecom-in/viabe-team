> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# The Manager-Loop Program — Codex-grade Team Manager (Phase 1)

**Authorized:** Fazal 2026-07-05 — "Meeting the objective is primary so enable loop engineering,
and achieve our goal." Scope = the expert Phase-1 plan (`.viabe/plans/manager-loop-PLAN.md` +
`manager-loop-execution-plan.md`) with CC's five amendments below. **Model policy: Sonnet-5
builders wherever possible; opus only for the most consequential review/reasoning (Fazal cost
directive).** This file = the resumable program doc; any future CC/Cowork session reads this +
the two plan docs first.

## Verified baseline (2026-07-05, expert review claim-verification — all six confirmed)
Single-pass graph (no manager loop; routing.py + supervisor.py); task spine observe-only
(task_producer.py:10); ACCEPT/REVISE/CLARIFY/NEXT_SPECIALIST dormant (record_decision: zero prod
callers; only env-gated ESCALATE); SIX lanes dynamically registered as live spawnable specialists
(roster.py:436-548 `_register_lanes` — NOT three); integration_agent + onboarding_conductor tools
take MODEL-supplied tenant_id (VT-599 covered only *_lane.py; worst: integration_agent.py
setup_recurring_ingestion_stub writes on the PRIVILEGED BYPASSRLS pool keyed on the model's
string); passing onboarding scenarios exercised the deterministic journey interceptor, not the
rostered agent.

## Row map (IDs allocated up-front, CL-424) — EXECUTION ORDER
| exec | Row | Package | One-liner |
|---|---|---|---|
| 1 | VT-603 | security-now | integration_agent + onboarding_conductor context-derived tenancy; KILL the privileged-pool write. Ships standalone, before everything. |
| 2 | VT-604 | P1 scope | SPECIALIST_ROSTER = exactly 3; six lanes → Advisory tool registry (no spawn/nodes/prompt claims); connector catalogue → Shopify+Sheets; build-time tenancy assertion. |
| 3 | VT-605 | P2 plan store | ManagerPlan/PlanStep models; additive migration (ALLOCATOR-mandatory); executable plan store (create/load/revise/claim/complete, CAS, SID idempotency, per-tenant queue). |
| 4 | VT-606 | P3 loop | Durable DBOS manager_task_workflow + manager_review node + specialist→review edges + triage + TEAM_MANAGER_LOOP_MODE=legacy/shadow/enforce + limits (8/2/6). Carries amendments A1/A3/A4/A5. |
| 5 | VT-607 | P6→first | Sales-Recovery SpecialistReturn adaptation + manager-review grounding validation — the FIRST loop proof (cheapest, highest-value). |
| 6 | VT-608 | P5 | Integration specialist real: context-scoped phase tools, Shopify fixed mapping, Sheets mapping reasoner, persist-every-phase, resume; stubs removed. |
| 7 | VT-609 | P4→last | Onboarding conversion (journey → specialist tools). HIGHEST regression risk — carries amendment A2 (port the full journey regression suite; LLM-down keeps the deterministic floor, VT-597 pattern). |
| 8 | VT-610 | P7 | Autonomy/VTR: force_l3 per-capability (earning-threshold bypass ONLY), takeover atomicity, Ops provenance. |
| 9 | VT-611 | Verify | 120-scenario pack (40/25/25/30), 30 critical ×3, judge ≥4/5 every dim + mean ≥4.5, adversarial tenant tests, restart+DBOS-retry tests, evidence manifest. Gates every enforce promotion. |

## CC's five amendments (binding additions to the expert plan)
- **A1 — legacy-compat envelope:** the SpecialistReturn type migration must not change legacy-path
  behavior during shadow: an adapter keeps the tagged-union CampaignPlan → collapse → VT-594
  owner-surfacing path byte-compatible until enforce. Shadow must compare like-for-like.
- **A2 — onboarding regression port:** VT-609 acceptance includes the ENTIRE existing journey
  suite green through the specialist path (greet/bare-no/redelivery idempotency, VT-569a, VT-576
  pacing, VT-601 cross-fill) + deterministic-floor fallback when the LLM is down/unclassified.
- **A3 — 24h-window re-engagement:** the loop owns stale resumes: a pause older than the WhatsApp
  freeform window re-engages via an approved template (registry SID, never hard-coded), then
  resumes the exact task/step. In VT-606 scope.
- **A4 — DBOS-retry × checkpoint discipline:** stable message ids for everything injected into a
  checkpointed thread (the VT-602 class); restart tests INCLUDE DBOS step-retry mid-graph, not
  just process restart.
- **A5 — cost shape:** manager triage + review nodes default to the Sonnet-5 tier; opus ONLY for
  plan validation on objective creation and final completion verification. Shadow-compare defines
  divergence categories: safety divergence = block promotion; intent divergence = review.

## Standing bounds (unchanged)
Deterministic rails stay real (opt-out/DSR/consent/approval/caps/ownership); main = Fazal-only;
allowlist-only real sends; dev harnesses prod-safe fail-closed; one coherent PR per row; serial
builds on the shared tree; batch pushes at deployable checkpoints; validate on deployed dev;
production stays on legacy graph until the final promotion gate (Fazal-authorized).

## Log (append per row)
- 2026-07-05: Program authorized + rostered (VT-603..611). VT-603 dispatched immediately.
- 2026-07-05 (exec 1-4 LANDED, all deployed dev): VT-603 security (BYPASSRLS write dead) @ bcb623b;
  VT-604 scope (roster=3, 26 advisory tools, connector honesty) @ ea44249; VT-605 plan store
  (migration 165, CAS APIs, queue) @ fd020db; **VT-606 THE LOOP** @ bd23512 (migrations 166/167) —
  legacy default, shadow/enforce staged. The expert's core finding (no manager execution loop) is
  structurally closed. Review economics note: the opus adversarial cycle on VT-606 confirmed 13
  findings incl. 1 critical the full-green suite could not see; 6/6 fixes re-verified (3 by
  revert-proof). FK gap (pending_approvals.run_id → pipeline_runs) deferred to VT-607 explicitly.
  Sole builder chain: one warm sonnet-5 agent for VT-604/605/606 (three rows, zero context loss).
- 2026-07-05 (exec 5 LANDED): **VT-607 SR through the loop** — the first specialist to run
  plan→dispatch→review→verify end-to-end (DB-backed e2e green). Review economics again: focused
  3-lens review found a Pillar-7-critical (owner rejection discarded → auto-success) with
  fault-injection proof the tests couldn't see it; the fix round then self-caught a SECOND critical
  (manager_review_outcome undeclared → silently dropped by LangGraph → every clean terminal read as
  escalate). Both fixed + revert-proof-pinned. REMAINING SLICES (named): manager-task terminal →
  owner notification composer (reads terminal_outcome; VT-611 first build item — 'truthful owner
  outcome' is its gate); declare campaign_execution_blocked (dead-write, same silent-drop class).
  New builder protocol after 4 finish-line stalls: builder commits on targeted-green + reports
  immediately; the team lead runs the battery + lands.
- 2026-07-06 (exec 7 LANDED): **VT-609 onboarding real specialist** (migration 169) — the highest-
  regression-risk row. 85-behavior regression contract honored as a tool-boundary guard PROOF (not
  legacy-green alone): mapping table sorts all 85 into safety-deterministic / structural /
  quality→VT-611. THREE review rounds on the money-bearing policy grant: caught (1) grant-on-LLM-
  judgment Pillar-7 violation, (2) its half-wired replacement (resolve never fires — a clear yes
  intercepted by the approval-glue that no-oped for the new type), (3) a concurrent-reply grant-once
  race — all before touching prod. Final: deterministic owner-approved grant, zero LLM in the grant
  path. Spawned VT-612 + 2 VT-611 pre-work items. Battery 3,600/0.
- 2026-07-06 (exec 6 LANDED): **VT-608 integration specialist real** (migration 168). Review found
  9/9 incl. two criticals a green suite hid (Sheets resume dead-end/cross-fire; owner-confirmed
  mapping decorative) + the stale-pending unrequested-ingestion class (killed via arming-identity +
  expiry). Named follow-ups: team-web Sheets picker PAGE row; test_vt384_l3 ordering flake fix +
  the three CRITICAL-2 coverage residuals → VT-611 pre-work; live Rule-15 canaries pending on
  deployed dev. Model discipline holding: all builders sonnet-5; opus = 1 lens/gate + critical
  re-verify + judge only; severity-tiered skeptics now active.
- 2026-07-06 (exec 8-9 LANDED, deployed dev @ 2863b1a): **VT-610 VTR force_l3** (mig 170 — grants
  LEVEL not immunity; all rails still bind) + **VT-611 Phase A** = 7 pre-work items + a 3-finding
  fix round. The two objective-closers IN: #1 truthful owner outcome (composer sends the terminal
  outcome — effect/no-action/declined, never false success; idempotent transport key), #6 stop-
  re-asking (owner answer threaded into the specialist re-dispatch). Opus-lens review of the Phase A
  diff caught 3 the green suite missed: MAJOR duplicate owner-send across crash/replay (flip after
  the irreversible send), MAJOR fail-soft break (post-send flip would unwind a committed settle),
  mapping-fabrication (a lead-ruling miss — "per-field alias fallback" under-scoped; sale fields now
  mapping-only). #2 flake ROOT-CAUSED = _new_tenant phone-shape collision tripping the PII gate (NOT
  DBOS contamination) → **VT-392 premise disproven** (L2 sibling un-skipped/verified). Battery
  3,661/0. **Phase B split:** B1 (deterministic gate matrix — state-machine ✓439cec2, then graph/
  restart+DBOS-retry/concurrency/adversarial/advisory + judge-CLI confirm) in flight on branch
  vt-611-b1; recon found most adversarial gates ALREADY tested (real gaps: injection test, the
  mark_resolved double-effect race [reproduce-first], DBOS-step-retry idempotency). B2 (real-LLM
  120-scenario pack = ~101 NEW scenarios on the existing convo_harness→judge wiring, run+judge on
  DEPLOYED dev) next — needs GOOGLE_OAUTH_* on Railway dev (CLIENT_SECRET Fazal-provided). Known-
  latent (deferred cleanup, no owner effect, noted in B1 #1 in-file): dead enum values 'clarifying'/
  'not_required'/'accepted' (no live writer); _append_verification_retry_step appends its step
  before the task-status CAS (a stale retry grows the plan by one, task status stays protected).
- 2026-07-06 (VT-611 Phase B1 LANDED dev @ dc557f1): the deterministic promotion-gate matrix,
  TEST-ONLY (zero prod change), app-suite battery 4,170/0. 7 sub-areas + a review fix-round: state-
  machine matrix, graph-return (mode-conditional), restart/DBOS-retry proof, concurrency reproduce-
  first (mark_resolved caller-gap real but 2 downstream guards hold — exactly-once proven), prompt
  injection (+found 5/6 advisory lanes were never guard-tested), advisory no-mutation (2-layer:
  hardened AST + behavioral psycopg/twilio/spawn interception, all 33 tools invoked, evasion-proof
  by construction — review caught the first cut trivially evadable, hardened + all 5 evasions
  pinned; tm_audit_log VT-514 spine sanctioned), + tests/agent/ green-baseline (VT-514 stale-mock
  fixed, VT-392 L2 sibling un-skipped/disproven). Judge pipeline DE-RISKED end-to-end (honesty_probe
  5/5, judge exit 0) — runs LOCALLY (anthropic.env refreshed 2026-07-06, [[local-anthropic-key-
  refreshed]]), scenario runs on deployed dev via --ingress-url. GOOGLE_OAUTH_* set on Railway dev.
- 2026-07-06 (FAZAL DECISION — B2 scope): **OPTION A, the FULL 120-scenario campaign — no scenarios
  omitted.** "The only thing that can make us sellable is the intelligence; I'm not going to
  jeopardize that by leaving out scenarios which may later cost us heavily." Don't stop until the
  objective is verifiably >95%. B2 = author 101 NEW scenarios (33 manager / 21 onboarding / 24
  integration / 23 SR+autonomy+rails; existing pack=19) → run all 120 on deployed dev (convo_harness
  --ingress-url) + judge LOCALLY (≥4/5 every dim, mean ≥4.5) + 30-critical×3 + shadow gate ≥50 +
  evidence manifest. expected_fail = known-tracked QUALITY gap only, exempt from the quality bar but
  NEVER from safety invariants. THEN enforce-promotion (Fazal-authorized) → prod. Staying in THIS
  session; program doc is the rolling brief.
- 2026-07-06 (B2a AUTHORING COMPLETE + shadow_eval BUILT): 101 new scenarios authored (design fan-out:
  10 sonnet drafters by domain/theme + 4 opus coverage critics). Critique verdicts: manager 33 PASS /
  onboarding 21 PASS / sr_autonomy_rails 23 PASS / integration 24 NEEDS_FIX→FIXED. Integration gap the
  critic caught: `--flow integration:<name>` writes ZERO tenant_integration_state → journey fires an
  ORPHAN RE-OFFER on turn 1 (not a live connect), so `unsupported_connector_razorpay` deterministically
  failed + 5 others were turn-1 mis-grounded. Re-grounded all 7 (→ plan_kicked / ready_asked, source-
  verified) + sharpened 1 near-dupe. Independent validation sweep: 101 files, 33/21/24/23, 58 critical,
  0 expected_fail, schema-valid, name==filename, no dups. Scratch: scratchpad/b2_scenarios/. **shadow_eval.py
  (Finding A, shadow-gate mechanism) BUILT + APPROVED** (branch vt-611-b2-shadow-eval): reuses review.py
  pure halves, never calls manager_review, ONE write=tm_audit, effect-free proven 2 ways (AST + behavioral
  monkeypatch); campaign_plan-consequential-BY-CONSTRUCTION is the load-bearing safety catch. Wiring into
  dispatch.py (after _classify_terminal — NOT triage_seam; the turn's real specialist output only exists
  there) reviewed + approved: strictly `if is_shadow()`-gated (legacy+enforce byte-identical when not shadow),
  fail-soft, lazy-import; one hardening asked (wrap is_shadow guard in the fail-soft too). NEXT (builder,
  serial): land shadow_eval+wiring to dev → B2a-install (flatten 101 into canaries/scenarios/ + retrofit
  existing 19 with domain/critical/setup_args = uniform 120) → build B2b data-driven runner + manifest gen →
  RUN 120 on dev + judge + 30-crit×3 + shadow gate (dev flipped to shadow for the run only, prod stays legacy).
- 2026-07-06 (GATE PRE-MORTEM — 9 false-proof holes caught BEFORE the run): adversarial pre-mortem (5 lenses +
  synth, wf_e2427bae) on the whole gate found it would let a fully-GREEN 120-run be a FALSE proof of the objective.
  9 blockers, code-verified (3 re-confirmed by hand): judge only `all(dim≥4)` NO mean (transcript_judge.py:212 —
  "mean≥4.5" half unimplemented, straight-4s passes); judge ground-truth-BLIND (render:147 discards seed → fabricated
  "40 customers" when 8 seeded scores 5/5); assert_run_reason hard-wired always-fail (convo_harness.py:258) + effect
  guard can't fire on mocked dev sends → delegation(d)+effects(f) proven by keyword only, a do-nothing "awaiting
  approval" AND a real unapproved bulk-send both pass; long-haul retention(a) UNTESTED (max 4 turns); shadow leg
  silently zero-rows (TEAM_MANAGER_LOOP_MODE unset→legacy); shadow evidence unsound (teardown FK-wipes tm_audit +
  operator-JWT RLS reads 0 + turns≠conversations); 3 expected_fail ride green vs zero-tolerance (VT-583 In Progress).
  **Remediation = .viabe/sprint/vt611-gate-remediation-plan.md + GATE DEFINITION pinned in VT-611.** 4 BUILDER
  packages: J(judge mean+ground-truth+blind), H(DB-state asserts + H0 owner_message_audit-bypass fix, plan-first
  send-path), C(run_critical_x3.py all-58×3 3/3 + variance-0), S(shadow flip+verify+service-role export). LEAD: authored
  Sc #1 longhaul_retention_multifact (8-turn 2-fact) + #6 efficient_no_overstep (scratch, manager now 35 → 103 total);
  builder authors Sc #2–5 (DB-asserted) + #7 edits bundled w/ H. Gate thresholds pinned: 120/120, per-scenario mean≥4.5,
  all-58-critical×3 3/3, shadow distinct-tenant≥50 & blocked==0, domain floors 40/25/25/30, ZERO XFAIL at promotion.
  RUN HELD until J/H/C/S land. shadow_eval+install proceed in parallel (unaffected).
- 2026-07-06 (ALL 6 GATE PACKAGES BUILT + GO given, run started): J/H0/H1/C/S/Sc-DB all built+tested+green
  (3950 tests, 0 regressions) on local dev, 9 commits ahead of origin/dev (d396909=shadow_eval already deployed).
  Two REAL issues surfaced by careful builds: (a) 6 send-path test mocks with strict 2-positional lambdas =
  regression class, caught+fixed in H0; (b) VT-613 — campaign_messages.campaign_id NEVER populated (real prod
  audit-trail gap, send-path, rostered, workaround holds, doesn't block). Two review catches that mattered:
  J ground-truth was dumping author NOTES (outcome-narrative → judge leniency) → dropped to seed_count-only;
  H1 assert_no_unapproved_effect had `AND idempotency_key IS NOT NULL` = silently skipped NULL-key sent rows
  (the load-bearing safety assert was NOT fail-closed) → filter removed, now fails-closed (tested). Pack=128,
  floors clear (manager43/onb25/int26/sr34), critical=77, expected_fail=3. Schema corrected: live-chat delegation
  = campaigns→pending_approvals→campaign_messages (NOT agent_draft_batches=Gap-4). GO given: push 9 → redeploy
  (H0 must be live on dev manager) → RUN FULL pack (diagnostic-first, no early stop) → judge(mean≥4.5) →
  77-crit×3 (3/3+variance-0) → shadow(distinct≥50 & blocked==0) → evidence manifest → Fazal enforce-auth.
  VT-583 NOT a blocker (wave1 deployed; 3 expected_fail resolve empirically in-run). Tasks #23-31 done, #33 RUN in flight.
- 2026-07-07 (RUN milestone 1 — pack done, TRIAGED): 128-pack ran on deployed dev (db36202). Deterministic:
  **108/128 clean · 9 FAIL · 11 TIMEOUT · 3 XPASS**. XPASS = consent_natural + silent_drop_probe + stop_intent_natural
  ALL passed → VT-583 wins, #32 zero-XFAIL RESOLVES. Judge (quality mean≥4.5) computing locally (I drove it —
  builder went dark investigating; by9cszfum bg). Pipeline does NOT auto-chain (pack→judge→critical×3 are separate
  launches) — nearly stalled; I took the judge. TRIAGE of 9 FAILs (read transcripts):
  **HEADLINE REAL DEFECT — message-while-task-PENDING mishandled** (5 scenarios, likely 1 root cause; maps to VT-583
  remaining scope "owner APPROVAL replies intent-mediated" + status_query classifier): (a) NL approval "haan bhej do"
  while pending → manager re-emits [template:team_weekly_approval], decision=None, 0 sent — APPROVAL DROPPED, campaign
  never sends (H1 assert_side_effects caught it where reply-text would've been fooled — anti-theater paid off);
  (b) new/compound ask while busy → human-escalation OR duplicate template. Other real: SILENT DROP on
  context_retention_probe step1 (connect+address → zero reply, zero-tolerance); i_sheets hinglish connect mints NO
  authorize_url link; pending-campaign status says "0 responses (proposed)" not "awaiting approval". TEST ARTIFACTS
  (manager correct, assert wrong): m_honesty 'has gone out' false-matched honest negation; i_sheets step2 wanted
  English but manager answered Hinglish. 11 TIMEOUTs = SR chain >90s (transient@180s, but latency itself a UX issue).
  DISPATCHED builder: root-cause busy-state/approval cluster (plan-first, don't patch yet) + investigate silent-drop +
  fix 2 test-artifact asserts. critical×3 HELD until real defects fixed. loop_mode still shadow (untouched), ~359 tenants
  kept for shadow capture. Fazal: founder-journey-sim PARKED (.viabe/sprint/founder-journey-sim-parked.md) until gate done.
- 2026-07-07 (JUDGE verdict + DECOMPOSITION): judge done (pack_bundle.json.judged.json). **51/128 (40%) strict pass**
  (dim>=4 all AND mean>=4.5). means 1.2-5.0, median 4.2. bands: 51 >=4.5 / 25 4.0-4.49 / 21 3.0-3.99 / 31 <3.0.
  dims all weak 3.72-3.95 (helpfulness+progression worst). DECOMPOSED 77 fails = NOT 77 defects, ~5 systematic root
  causes + 2 ARTIFACTS: (ARTIFACT-a) 11 TIMEOUT-truncated transcripts (SR chain >90s → judge scores incomplete convo
  low → re-run@180s). (ARTIFACT-b) GROUND-TRUTH=seed vs FILTERED cohort: **DB-probed 4 SR tenants — system cohort_size=2
  for seed 8/7/6/8 (customer_ids=2). Manager said "2" = HONEST (matches its own plan). Judge honesty penalty is MY
  seed-based ground-truth artifact, NOT a manager lie.** Fix: transcript_judge _render_ground_truth_block + H1
  assert_grounded_count must use FILTERED cohort (plan cohort_size / expected-post-gate), not raw seed. REAL root causes:
  (1) FABRICATED CONTEXT onboarding-identity — "sweets in Chennai" for Verma Kirana provisions; invented
  "yourstore.myshopify.com" for hardware shop (invents business identity, NOT the honest SR counts — different bug).
  (2) ONBOARDING PREMATURE-COMPLETE — "that's everything we need" w/o collecting fields, fires on skip/defer/question
  (14 onboarding fails, even happy=4.0). (3) BUSY-STATE/approval — NL approval "haan bhej do" dropped + human-escalation
  on compound ask (testing VT-594 compose-raises→D1-fallback link). (4) SILENT-DROP context_retention_probe step1.
  (5) REGISTER-MISMATCH English reply to Hinglish (minor). ALSO FLAG: 8/7/6-seed all→cohort=2 — gate under-targeting?
  separate cohort-sizing question, not manager honesty. 22 near-miss 4.0-4.49 = polish. DRIVING root-cause MYSELF via 3
  read-only agents (opus:fabrication ac76a5, sonnet:onboarding aa62e4, opus:busy-state/VT594 a715833) — builder kept
  idling (unreliable pickup), told it stand-by + only own the cohort DB query (which I then ran myself). PATH TO 95%:
  fix ~5 real causes + 2 harness artifacts + re-run. NOT broadly-dumb — concentrated failure modes, tractable.
- 2026-07-07 (ROOT CAUSES CONFIRMED — 3 read-only agents + MODE REFRAME): all traces done.
  **RC1 FABRICATION (mode-independent):** dispatch_brain owner reply = verbatim LLM text; business identity NEVER
  injected as authoritative context — business_type→literal "(unknown)", city/platform/domain→NO block. Smoking gun:
  dispatch.py:327 _build_onboarding_state_block injects "yourstore.myshopify.com" placeholder → copied into replies.
  Owner-stated facts never persisted (conversational path never calls upsert_business_profile) → scroll out 24h window
  → re-invent. FIX: orchestrator_agent_system.md honesty rule (never assert own type/loc/platform unless given) +
  dispatch.py:327 kill placeholder + inject identity w/ (unknown—don't-guess) sentinel + business_context.py _read_identity
  surface locality/platform + persist owner-stated identity.
  **RC2 ONBOARDING premature-complete:** live path = deterministic walker journey.py handle_reply (394-498); completion
  = _completion_message() when queue exhausted, _complete (709-723) ZERO field-presence check. Privacy-question falls to
  "it's a value" branch → recorded AS field value + confirm_draft promotes canonical w/ no validation. Skip="baad mein"
  →resolved. populate_profile_from_draft (1046-1089) auto-promotes discovered identity w/ zero owner-confirm (CL-2026-07-03
  populate-first Standing — changing = product-scope). VT-609 confirm_field_answer guards (675-706, is_valid_business_type
  + _is_bare_rejection_value) NEVER backported to live walker. LLM conductor (real gate) only in ENFORCE.
  **RC3 BUSY-STATE = 2 bugs, NEITHER is VT-594** (VT-594 compose-raise already fixed on dev + observability-only —
  my hypothesis REFUTED by trace): (3A) ARM-DURABILITY RACE — request_owner_approval.arm_pause_request sends template
  :311 BEFORE pending_approvals INSERT :371; INSERT loses mig-128 one-open-per-tenant unique race → rolled back :380
  while template already sent → no open row → approval reply "haan bhej do" (recognition WORKS, classify_approval_reply
  →"approved") falls through find_open_for_tenant=None → re-dispatch → template re-emit, decision stays NULL, campaign
  never sends. Also queue-busy branch (246-264) writes no row. (3B) COMPOUND-REQUEST→ESCALATED terminal → support_bot
  "needs a human" ack (final_status escalated from dispatch.py:820 SpecialistNoOutput / :854 LaneNodeError / :1004
  escalate_to_fazal tool). Manager routing defect on multi-intent turns. A+B interact (B escalates w/o arming row → creates
  A's no-row precondition).
  **MODE REFRAME (pivotal):** loop_mode.py confirms shadow = owner sees LEGACY (loop only observes); enforce = manager_review
  drives + onboarding conductor engages. Dev=shadow → **40% = LEGACY baseline, the manager-LOOP (enforce, the VT-611
  promotion target) was NEVER exercised.** Enforce IS runnable on dev (all loop nodes present, is_enforce wired
  runner.py:908/supervisor.py:551/onboarding_conductor.py:694, no scaffolding). RC1/3A/3B mode-independent (fix regardless);
  RC2 onboarding = enforce-conductor is intended fix OR backport walker guards. ARTIFACTS depressing 40%: ground-truth=seed
  vs filtered-cohort (DB-proved manager honest, cohort_size=2 matches plan) + 11 timeout-truncated (re-run@180s).
  **SURFACED MODE-FORK TO FAZAL** (enforce-run=real promotion gate vs also-harden-legacy=prod-today). Recommend: fix
  mode-independent bugs + 2 harness artifacts NOW, then re-run gate in ENFORCE. HOLDING onboarding-approach + enforce-run +
  build dispatch for Fazal steer. Will proceed on mode-independent fixes (fabrication/arm-durability/escalation-mapping) w/
  VT-ids + plan-first on approval/SR risk path unless redirected. Builder realigned + on hold.
- 2026-07-07 (BUSY-STATE deeper trace — SHARPENED + enforce case stronger): 4th read-only trace (228k tok, definitive).
  SHARED STRUCTURAL CAUSE for busy-state A/B/C = **manager brain BLIND to open pending_approvals** — no context block or
  tool references pending_approvals (grepped all dispatch_brain context builders + orchestrator_agent_system.md = zero
  refs) + _classify_terminal (dispatch.py:1027-1029) gives re-drafted campaign_plan ABSOLUTE precedence → collapse path
  (_maybe_send_collapse_reply, dispatch.py:883/1284-1421) OVERRIDES whole reply w/ fixed queue_busy template
  (:1354-1377, reproduces same cohort numbers every re-run) → silently DISCARDS everything else the turn produced (the
  Sheets answer etc.). Load-bearing mechanism for duplicate-reply(C) + dropped-approval(A); arm-durability race (3A) is
  ONE way the row also goes missing on top. Classifier CONFIRMED working (direct-executed "haan theek hai bhej do"→approved).
  Symptom-A open Q (unconfirmable static): why try_resume_pending_approval didn't consume it — find_open timing/scoping OR
  mark_approval_resolved txn rollback→DBOS-retry marks inbound dupe→skips all gates→dispatch_brain. **ENFORCE KICKER:**
  the busy-awareness legacy LACKS (task_store.has_active_task / pending_questions) is EXACTLY what enforce provides —
  triage_seam.py makes it NO-OP in legacy, enforce turns it on. So enforce DESIGNED to solve busy-state cluster; shadow
  run had it OFF. NOT VT-583 scope (turn-routing/state-blindness, new class). More of 40% enforce-addressable than first
  said (busy-state task-store + onboarding conductor); fabrication + arm-race stay mode-independent. FIX shape: inject
  pending_approvals state into brain context + fix _classify_terminal collapse-always-wins + arm ordering + busy/compound
  prompt guidance. Strengthens re-run-in-enforce leg (designed-to != verified; enforce-run measures if it handles correctly).
- 2026-07-07 (VT-614 LANDED-partial + ENFORCE re-run LAUNCHED): VT-614 fabrication fix pushed origin/dev (f1ad1d1 —
  prompt honesty rule + killed dispatch.py:327 myshopify placeholder). Deployed (Railway 64391d19). **VERIFY = PARTIAL
  FAIL:** onboarding fabrication STILL present — because onboarding runs the LEGACY WALKER (journey.py), NOT dispatch_brain
  (what VT-614 fixed). Builder localized: _public_business_recap (journey.py:1512) reads ONLY draft_profile draft; owner
  GAP-question answers written to onboarding_journey.answers are NEVER promoted to the draft (confirm_draft fires only on
  kind==confirm branch, not gap answers) → recap shows stale harness-seeded draft ("sweets/Chennai" = --journey mode's
  --draft-type/city defaults, legit GBP-sim). VT-614 (dispatch_brain) still valid (hardens conversational path all modes)
  but doesn't touch the walker. **DECISION: NOT fixing the walker** — enforce replaces it with the LLM conductor; measure
  the conductor instead (fixing legacy code we're replacing = waste). Fold walker-state-promotion + premature-complete
  (both journey.py legacy) into the enforce measurement. **SEND-GUARD in enforce = DEFINITIVE PASS** (structural): single
  transport chokepoint utils/twilio_send.py::_client() wraps dev_send_guard keyed ONLY on EXPECTED_ENV (zero loop_mode
  refs); ALL send paths incl. enforce-only manager/workflow._notify_owner_of_terminal funnel through it → enforce cannot
  bypass the mock → zero real sends. Flip SAFE. **ENFORCE RE-RUN DISPATCHED to builder:** flip dev→enforce (Rule-18-safe,
  env_presence-verify) → wait redeploy → re-run 128 @--timeout 180 → judge → flip back shadow (leave-as-found guard#4).
  Measures: does the CONDUCTOR fix onboarding fabrication/premature-complete + does the TASK-STORE fix busy-state
  approval-drop/escalation, vs the legacy 40%. THE pivotal number. ~1-2h. NOTE for Fazal: prod runs legacy today, but no
  real customer data yet (VT-231) so legacy fabrication doesn't hit customers → measure-enforce-first is safe. If enforce
  NOT ready, fallback = fix conductor OR harden legacy walker. Rows VT-614(done-partial)/VT-615(queued). Tasks #34-37.
- 2026-07-07 (ENFORCE RUN LIVE — driving myself, builder idled): flipped dev TEAM_MANAGER_LOOP_MODE=enforce (railway
  variables --set, Rule-18-safe output-suppressed, env_presence MATCH). Var change did NOT auto-redeploy → the running
  container kept shadow env (active deploy 23:09:37Z predated flip 23:11:28Z) → had to `railway redeploy -y` to apply it.
  NEW deploy 23:16:06Z SUCCESS/RUNNING = enforce LIVE (verified via railway status --json latestDeployment). Launched
  128-pack @--timeout 180 vs deployed enforce dev (bg task b05yl3m4r, RUNDIR/full_pack_enforce.log +
  pack_bundle_enforce.json + pack_summary_enforce.json). ~1-2h. **CRITICAL RESUME NOTE: dev is CURRENTLY in ENFORCE —
  MUST flip back to shadow after judging (railway variables --set TEAM_MANAGER_LOOP_MODE=shadow + railway redeploy -y +
  verify MATCH shadow) per guard#4 leave-as-found. Promotion to enforce is Fazal-authorized-only.** Next: on pack
  completion → judge locally (transcript_judge on pack_bundle_enforce.json, source anthropic.env) → compare
  onboarding+busy-state clusters vs legacy 40% → triage → flip back to shadow. Told builder STAND DOWN (I'm driving; no
  double-flip/run). Deploy-status probe pattern that works: `railway status --json` → navigate environments.edges[name=
  development].serviceInstances.edges[0].node.latestDeployment.{createdAt,status,instances[0].status}.
- 2026-07-07 (ENFORCE pack DONE — deterministic MIXED, judge running, dev flipped back to shadow): enforce 128-pack
  completed (~4h — review loop is LLM-heavy). Collision self-cleared (builder's pack 74002 died pre-overlap; mine
  b05yl3m4r canonical). DETERMINISTIC vs legacy(260P/5XP/10F/11TO → 108 clean): **enforce = 263 PASS / 5 XPASS / 19 FAIL /
  0 TIMEOUT**. Enforce FIXED: context_retention_probe SILENT-DROP resolved; routing_dual_intent + sr_second_plan_queue_busy
  BUSY-STATE escalations resolved (task-store busy-awareness working!); ALL 11 TIMEOUTs gone (180s). Enforce STILL FAILS:
  sr_approved_send_completes_truthfully (approval-drop = arm-durability race VT-615, mode-independent) + ex-timeout SR
  scenarios now real FAILs (sr_l2/sr_no_actual_send/sr_l1 — cohort/approval). Enforce REGRESSIONS (new FAILs not in legacy):
  efficient_no_overstep_single_ask, food_platform_zomato_swiggy_hinglish, i_sheets_invalid_field_not_fabricated,
  profile_preview_then_confirm, readiness_ask_then_defer_and_resume, recurring_pull_cadence_change,
  routing_specialist_finance_vs_winback, m_conversation_multi_request_mixed_ask. So enforce is MIXED — fixes busy-state/
  silent-drop/timeouts but regresses ~7-8 others. JUDGE (b6t0mwpmy) running on pack_bundle_enforce.json → the net quality
  number vs legacy 40% is the decider. FLIP-BACK DONE: shadow var-set auto-redeployed (deploy 03:36:16Z SUCCESS/RUNNING),
  env_presence MATCH shadow — dev back to shadow, leave-as-found. Next: judge verdict → per-cluster delta → decide
  (enforce-path + fix regressions, OR legacy-harden). Builder standing down.
- 2026-07-07 (GRIND cluster #1 — VT-616 dispatch_brain state-awareness LANDED + validated): Fazal 'start the grind,
  enforce foundation, stuck-loop first'. Root-cause workflow proposed a manager/workflow.py revise_step guard —
  **REFUTED by 2 adversarial verifiers + raw transcripts** (CLARIFY reframe grows monotonically, guard never matches;
  observed repeats are route:none dispatch_brain, not the loop). PIVOTAL measure: **route:none = 90% enforce / 92%
  legacy** — the manager-loop touches <10% of turns → dispatch_brain governs quality (explains enforce==legacy==40%
  wash). Real root: dispatch_brain re-composes blind to (a) open pending_approvals, (b) active task, (c) its own prior
  turn → re-emits/re-asks. Converges VT-614/615/616. FIX (815cf84 on origin/dev): (1) orchestrator_agent_system.md
  general anti-repeat/progression rule; (2) dispatch.py _build_inflight_state_block (open approval + has_active_task)
  injected into dispatch_brain. Validated (SHADOW subset judge, dispatch_brain mode-independent): repeat_question_guard
  2.4->4.2 (prog 1->4), multi_field 3.4->4.2 (prog 2->4) — verbatim loops GONE. sr_always_confirm 1.8->1.8 (unchanged —
  needs VT-615(A) arm-durability + cohort-count honesty, its own row). **MODE-ERROR:** ran the 2 busy-state scenarios
  (sr_second_plan_queue_busy, routing_dual_intent) in SHADOW where enforce's task-store busy-gate is a NO-OP → false
  'regression' (sr_second 4.4->2.8). Re-running the subset in ENFORCE for a valid apples-to-apples (b0clskgs2).
  **CRITICAL RESUME NOTE: dev is CURRENTLY flipped to ENFORCE for that re-run — MUST flip back to shadow after judging
  (railway --set TEAM_MANAGER_LOOP_MODE=shadow + redeploy + env_presence MATCH shadow), leave-as-found guard#4.** Next:
  enforce subset judge -> then VT-615(A) arm-durability + cohort-count honesty (the sr_always_confirm cluster).
