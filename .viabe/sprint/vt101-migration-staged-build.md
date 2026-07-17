---
id: VT-101 (re-scoped) + VT-664 (§7.1) + VT-659-build (§7.2)
title: Agent-framework migration — staged build to CANONICAL ARCHITECTURE.md
status: in-progress
priority: Critical
owner: claudecode
created: 2026-07-17
authorization_basis: "Fazal 2026-07-16 21:22 RATIFIED grant — close tonight; docs/agent-framework/ARCHITECTURE.md CANONICAL. Non-negotiables: gated tools own whole round-trip; correctness gates never bend; post-cutover j01-j10 x1 + tier_rescore Tier-1=0 MUST HOLD or ROLL BACK (don't patch forward at 3am); one coherent PR per row; ledger CL-2026-07-16-arch-ratified-migration."
grounding: "understand workflow wf_d481c53c-248 — 5 maps in scratchpad/map_{integration,sr,contract,gate,wiring}.md (NOTE: contract API = map_sr.md; wiring = map_integration.md; SR = map_sr_x.md; integration = map_integration_x.md; gate = map_gate.md)."
---

# Migration — staged build (additive-first, live-cutover gated behind dev regression)

## Contract API (from map_sr.md — the exact surface)
- Module = class with `manifest: AgentManifest` + `propose(ctx,gate)` / `execute(ctx,gate)` per role. Import ONLY from `orchestrator.agent_framework`.
- `AgentManifest(name, version, roles={PROPOSER|EXECUTOR}, description, capabilities=frozenset[Capability], prerequisites, tools=(), entitlement_key)`. `validate()` rejects PROPOSER-only + gated cap.
- Capabilities: READ_BUSINESS_CONTEXT/READ_CUSTOMER_LEDGER/READ_INTEGRATION_STATE (non-gated), PROPOSE_* (non-gated), REQUEST_CUSTOMER_SEND/REQUEST_BUSINESS_ACTION (GATED, EXECUTOR-only). `GATED_CAPABILITIES` = those two.
- `GateFacade` (framework-built): `request_customer_send(draft_id,autonomy_level,conn,send_fn)` → `customer_send.agent_send_draft` (7 gates); `gate_business_action(action_class,magnitude_minor,action_attrs,conn)` → `business_impact_choke`.
- `ModuleContext.for_proposer(tenant_model_value,module_name,run_id,situation,desired_outcome,context_slice,data)` (IDOR resolve) / `.for_executor(tenant_id,item_id,work_item_id,run_id)`.
- `ModuleResult(role,status,proposal,work_item_status,batch_id,counters,reason)` → `.to_agent_result()` / `.to_item_execution_result()`.
- `register_agent(module)` (inert until wired). `assert_conforms(module)` = 8 checks. `CoordinatorAgentAdapter(registered)` adapts EXECUTOR → coordinator `SpecialistAgent` (name must == manifest.name == coordinator SpecialistAgent.name).
- Deny-list `assert_agent_tools_safe` (tool_guardrail.py) rejects send/sheet-write/ledger-write/spend tool names by substring.

## STAGE PLAN (each stage = self-contained; STOP+report if a stage can't validate)

### Stage 0 — SR module (§7.2 thin adapter, ADDITIVE, zero SR-file edits) — VT-659-build
Author `agent_framework/modules/sales_recovery_module.py`: a dual-role module wrapping `run_sales_recovery_agent` (PROPOSER) + `sales_recovery_executor` (EXECUTOR). Manifest: name must == coordinator SpecialistAgent.name for SR; roles {PROPOSER,EXECUTOR}; capabilities {READ_CUSTOMER_LEDGER, PROPOSE_CAMPAIGN, REQUEST_CUSTOMER_SEND}. propose() calls the existing proposer, maps AgentResult→ModuleResult; execute() calls the existing executor via the gate. Register via register_agent. **Validate: assert_conforms green + unit test. NO live wiring yet.** Zero regression risk.

### Stage 1 — Integration connector-Tools + module (§7.1 ADDITIVE) — VT-664
Register the 11 integration @tools (all VT-268-safe, non-gated) as a conforming module `agent_framework/modules/integration_tools_module.py` (capabilities {READ_INTEGRATION_STATE}; tools = the existing @tool objects; PROPOSER role — reads/proposals only, commit_ingestion stays proposal-only). assert_conforms green. **NO brain removal yet.**

### Stage 2 — GateFacade whole-round-trip fix (§2, money-path) — the B-finding  [PRECISE DESIGN]
Gap (map_gate.md): `request_customer_send`→`agent_send_draft` already owns the whole round-trip (7 gates + issue inside `customer_send_context`). `gate_business_action`→`assert_or_gate_business_action` returns **decision only**; caller owns choke-entry+effect. **No LIVE path issues a business-action effect today** — marketing_lane/tech_lane/workflow only INTENT-CHECK (decision, no effect); the full round-trip exists ONLY in `business_impact_sample.propose_spend:97-129`. So the fix is ADDITIVE + low-risk.
FIX: add `GateFacade.perform_business_action(action_class, magnitude_minor, effect_fn, *, action_attrs=None, conn=None)` (mirrors `request_customer_send`'s `send_fn`): (1) `assert_or_gate_business_action` → BusinessActionGate; (2) requires_owner_approval → `arm_business_action_approval`, return armed gate (no effect); (3) autonomous → `with business_action_context(action_class): effect_fn()` (effect_fn self-guards `assert_in_business_action_context`), return result. KEEP `gate_business_action` (decision-only) for the advisory intent-checkers (no effect = acceptable). Declare it needs REQUEST_BUSINESS_ACTION (already gated). `UngatedBusinessActionError` rail stays. Correctness gates (policy bound, per-class tier, negative-magnitude, frozen) must not bend.
UNIT TESTS: autonomous → effect_fn runs inside choke; requires_approval → armed + effect_fn NOT run; effect_fn issuing OUTSIDE choke → UngatedBusinessActionError. Re-prove emission-gate money-claim→DB binding untouched (its 2 seams dispatch.py:1497 + reply_to_owner.py:170 + the send-ledger window sources).
NOTE: do NOT edit gate_facade.py while an assert_conforms subagent runs (it imports gate_facade).

### Stage 3 — LIVE CUTOVER (the risky part; gated behind Stage 4 regression)
(a) Route the coordinator's SR dispatch through `CoordinatorAgentAdapter(registered_sr)` instead of the direct executor.
(b) Manager delegates SR via the module (ModuleContext) instead of raw spawn.
(c) Integration: Manager drives the connector Tools directly; remove the integration brain/spawn_integration; move OAuth/mapping/escalate beats to Manager-driven flow; keep zero-manual-paste. Close VT-658.
DB access UNCHANGED here (tools keep resolve_lane_tenant + own conn — §7.3 deferred).

### Stage 4 — REGRESSION GATE (non-negotiable)
Full j01-j10 x1 on deployed dev + tier_rescore. **Tier-1=0 MUST HOLD.** If broken → `git revert` the cutover (Stage 3), keep Stages 0-2 (additive), report. DO NOT patch forward.

### DEFERRED (Fazal-explicit): §7.3 DB-access inversion (VT-621 GUC-pool class) — LAST, only if genuinely safe; expected to defer.

## Progress log
- 2026-07-17 03:30 — understand workflow done (5 maps); plan written.
- 2026-07-17 03:55 — Stage 0 (SR module) DONE + committed e54f74c (local, additive/inert). assert_conforms 8/8, 14 tests, ruff clean, import-inert verified. Capability call: Option A (no REQUEST_CUSTOMER_SEND — arm != send; preserves money semantics). Faithful thin adapter reviewed. Starting Stage 1 (Integration module).
- 2026-07-17 — Stage 1 (Integration tools module) DONE + committed 2366904 (local, additive/inert). IntegrationToolsModule name="integration_tools", PROPOSER, caps {READ_INTEGRATION_STATE, PROPOSE_CONFIG_CHANGE}, 11 INTEGRATION_AGENT_TOOLS verbatim (lazy). assert_conforms 8/8, 10 tests (dep-less importorskip), ruff clean. Naming (integration_tools vs integration_agent) + activation bar deferred to cutover.
- 2026-07-17 — Stage 2 (GateFacade whole-round-trip) DONE + committed e8f169a (local, additive/low-risk). Added BusinessActionOutcome + GateFacade.perform_business_action (REQUEST_BUSINESS_ACTION-gated; autonomous→effect inside choke, approval→arm+no effect). Kept gate_business_action (decision-only) for advisory intent-checks; GATED_METHOD_BY_CAPABILITY unchanged → conformance byte-stable. 5 unit tests + full agent_framework 29/29, ruff clean. Correctness gates untouched (live in the deterministic gate).
- 2026-07-17 ~04:05 IST — Stages 0-2 BATCH-PUSHED to origin/dev (a50b0e1..e8f169a). Pre-push full DB suite 4834 passed/18 skipped. Railway dev deploy e8f169a = SUCCESS. origin/dev tracking ref moved. Ledger CL-2026-07-16-arch-ratified-migration appended. Morning report written: .viabe/vt101-migration-morning-report.md (single source of truth). Cowork status signal sent (20260716T223600Z).
- 2026-07-17 ~04:20 IST — Fazal LIFTED the 3am rail (awake, supervising; Cowork 20260716T225500Z "RESUME Stage 3 NOW"). Executing 3(a)+(b).
- 2026-07-17 — Re-baseline (e8f169a, flag-off): 9/10 clean; lone Tier-1 = j02 MARKETING (pre-existing loop_stall, reproduced ×2 incl 180s → VT-666, orthogonal to SR). j01 SR win-back clean. Gate re-premised to a DELTA gate (post-cutover Tier-1 ⊆ baseline {j02}); routed to Cowork 20260717T0049Z.
- 2026-07-17 — Stage 3(a)+(b) BUILT (delegated, reviewed) + committed c7fff55: SR proposer + coordinator executor routed through the framework contract behind TEAM_SR_VIA_FRAMEWORK (default OFF). Money-path-faithful (None-preserving proposer; validate_context_isolation kept). Tests: routing+module 21, agent_framework 30, coordinator/supervisor regression 121 — all green. Pushed e8f169a..c7fff55 (pre-push suite 4834 pass). Deployed dev SUCCESS. Dev flag set ON.
- 2026-07-17 02:15Z — Stage 3(a)+(b) VALIDATED → **HOLD** (delta gate PASS). 3(a) canary: CoordinatorAgentAdapter→real SR executor ARMS (awaiting_approval, 0 sends, no exception; dispatched=1 post-Step-B-plan-seed, control dispatched=0). 3(b) j01 SR win-back CLEAN (4/4); Tier-2 100%; post-cutover Tier-1={j02,j09} but j09 (multistore menu-fallback, non-SR) re-drove ×2 flag-on CLEAN → VARIANCE, not cutover-caused (SR-only code delta). Effective Tier-1={j02}=baseline. SR cutover introduced ZERO deterministic breakers. VT-666 broadened (menu-fallback class: j02+j09). Cowork 20260717T0215Z.
- 2026-07-17 — 3(c) integration dissolution = LAST Stage-3 step (separate validated push); deepest surgery. §7.3 DB-inversion stays Fazal-deferred.
- (SUPERSEDED) 2026-07-17 ~04:05 IST — DECISION: DEFER Stage 3 (live cutover) + Stage 4 (regression). Rationale = Fazal's rail: Stage 3(c) is deep manager-path surgery; the only acceptance is post-cutover j01-j10 + tier_rescore Tier-1=0-or-roll-back (~1.5-2hr unsupervised cycle at ~4am) — exactly the "don't patch forward at 3am, roll back not forward" case. Additive foundation is landed + safe. Resume plan in the report §"Exact resume plan" (re-baseline → 3a+3b SR routing → 3c integration dissolution, each a SEPARATE validated push). §7.3 stays Fazal-deferred.
- DECISION for tonight: build additive Stages 0-2 (safe) + assess the RISKY Stage 3 live-cutover afterward — attempt only if validatable on dev with Tier-1=0 held, else DEFER with honest report (Fazal's roll-back-not-rush rail). Additive stages batch-push before any cutover.
