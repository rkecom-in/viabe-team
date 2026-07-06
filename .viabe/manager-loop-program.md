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
- 2026-07-06 (exec 6 LANDED): **VT-608 integration specialist real** (migration 168). Review found
  9/9 incl. two criticals a green suite hid (Sheets resume dead-end/cross-fire; owner-confirmed
  mapping decorative) + the stale-pending unrequested-ingestion class (killed via arming-identity +
  expiry). Named follow-ups: team-web Sheets picker PAGE row; test_vt384_l3 ordering flake fix +
  the three CRITICAL-2 coverage residuals → VT-611 pre-work; live Rule-15 canaries pending on
  deployed dev. Model discipline holding: all builders sonnet-5; opus = 1 lens/gate + critical
  re-verify + judge only; severity-tiered skeptics now active.
