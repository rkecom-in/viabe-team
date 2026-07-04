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
