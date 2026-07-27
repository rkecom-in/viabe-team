---
vt: VT-659
title: SR → agent-framework migration DESIGN (build deferred to post-VT-657 + post-Tier-1=0)
status: design-complete
author: claudecode (vt659-sr-design agent, read-only)
ts: 2026-07-16
depends_on: VT-657 (land first; rebase module onto stable SR seam)
---

# VT-659 — SR → agent-framework migration design

**Recommendation: thin dual-role adapter, additive + inert, cutover deferred.** ADDS one module file + one test file, edits ZERO of VT-657's files. Collision surface = nil until a deliberate, Fazal-authorized cutover step.

## 1. Framework contract (`orchestrator/agent_framework/`, VT-649/650 — NOT agent/roster.py)
A module = class with `manifest` + a role method per role.
- Manifest `agent_framework/manifest.py:28` `AgentManifest(name, version, roles, description, capabilities, prerequisites, tools, entitlement_key)`; `validate()` (:112) — gated (REQUEST_*) capability legal ONLY if EXECUTOR ∈ roles (:142); `prerequisites.agent == name` (:149).
- Roles `capabilities.py:23` `AgentRole{PROPOSER, EXECUTOR}`; `ROLE_METHOD` (:46) PROPOSER→propose, EXECUTOR→execute.
- Capabilities `capabilities.py:52` — non-gated READ_*/PROPOSE_*; gated REQUEST_CUSTOMER_SEND (:85)/REQUEST_BUSINESS_ACTION (:89) in GATED_CAPABILITIES (:95). Invariant: NO capability means "send directly."
- Protocols `protocols.py:38/54` `ProposerModule.propose(ctx, gate)->ModuleResult`; `ExecutorModule.execute(ctx, gate)->ModuleResult`.
- Context `context.py:39` `ModuleContext`; `for_proposer` (:73) resolves via `resolve_lane_tenant` (IDOR guard); `for_executor` (:114) trusts server tenant; fail-closed TenantResolutionError.
- Result `context.py:141` `ModuleResult(role,status,proposal,work_item_status,batch_id,counters,reason)` + `to_agent_result()` (:165)/`to_item_execution_result()` (:176).
- Trust boundary `gate_facade.py:49` `GateFacade`: `request_customer_send(draft_id,autonomy_level,...)` (:101)→customer_send.agent_send_draft; `gate_business_action` (:139)→business_impact_choke. Proposer facade STRIPS gated caps (`manifest.capabilities_for_role`, :85) → proposer lane structurally side-effect-free.
- Registration `registration.py`: `register_agent(impl)` (:184) 4-layer validation; `RegisteredModule.run(ctx)` (:68) dispatches by ctx.role; `CoordinatorAgentAdapter` (:266) adapts EXECUTOR→coordinator SpecialistAgent; `register_activation_prereqs` (:204) drift-guarded.
- Conformance `conformance.py:295` `assert_conforms(module)` — 8 checks incl proposer_gate_readonly, gated_capabilities_serviced.
- Reference `reference_plugin.py`; dual-role SR worked example in `agent_framework/README.md:45-66`. `default_registry()` empty → importing framework wires nothing.

## 2. Current SR map
### PROPOSER (Tier-2 pure — no DB/send/mutation)
- `agent/sales_recovery.py:761` `run_sales_recovery_agent(context, *, evaluator)->AgentResult`.
- Input `context_builder.py:218` `SalesRecoveryContext` (+ `dormant_cohort` :240). Built by `handoffs.py:129`. Live node `supervisor.py:222` `_sales_recovery_node` (runs `validate_context_isolation` :286, VT-73). Persistence+arm DOWNSTREAM in collapse node + approval rail, NOT the proposer.
- VT-651 server cohort: `_server_target_cohort` (:421) + `_construct_variant_payload` override (:689-717) — full eligible cohort, exclusion_list=[], VT-499 window (:385), VT-498 scrub (:491), VT-501 refs (:577).
- NOTE: `agent/sales_recovery_node.py` is a duplicate VT-32 wrapper; live node is supervisor's. Cleanup, not migration scope.
### EXECUTOR (coordinator sweep — ARMS, does NOT send)
- `agents/sales_recovery_executor.py:730` `SalesRecoveryAgent.execute_item(ctx)->ItemExecutionResult`; registered coordinator.py:135.
- Pipeline: `_owner_inputs_ok` (:576/:749 CL-425) → `tenant_is_sr_eligible` (:763 VT-421) → `detect_lapsed_customers` (:260, 45d) → per-bundle draft + `validate_draft_params` (:464) → `_persist_draft_batch` (:487) → ARM.
- ARM = money seam: `_try_l3_arm` (:684) enter_l3_hold, ELSE L2 `arm_agent_send_approval` (approval_glue.py:141); refusal→`_cancel_batch` (:534). Terminal awaiting_approval + batch_id. SEND is separate downstream (approval_resume→agent_send_draft / l3_hold→agent_send_draft).
### Shared choke (downstream, UNCHANGED by migration)
`agents/customer_send.py` `agent_send_draft` Gate 0..5 — keeps send semantics byte-for-byte.

## 3. Design — thin adapter (recommended, NOT full port)
Rationale: executor's consequential act is the ARM; framework's REQUEST_CUSTOMER_SEND sends IMMEDIATELY. Expressing arm as request_customer_send BYPASSES arm→approval→send = live send. Full port = extend framework w/ gated ARM cap = larger money-path change, OUT of launch scope.
- ADD `agent_framework/modules/sales_recovery_module.py` (new modules/ subpkg, NOT imported by agent_framework/__init__.py → framework import stays inert). Class `SalesRecoveryModule`:
  - manifest: name="sales_recovery", version="1.0.0", roles={PROPOSER,EXECUTOR}, capabilities={READ_CUSTOMER_LEDGER, PROPOSE_CAMPAIGN}, prerequisites=SR's existing AgentPrerequisites (verbatim from activation_registry.REGISTRY["sales_recovery"]), tools=(). (Capability question → §7.)
  - propose(ctx,gate): delegates to run_sales_recovery_agent; reads pre-built SalesRecoveryContext from ctx.data["sales_recovery_context"]; builds SelfEvaluateAdapter; returns ModuleResult(role=PROPOSER, proposal=agent_result.output). gate unused.
  - execute(ctx,gate): delegates to SalesRecoveryAgent().execute_item(AgentItemContext(...)); maps ItemExecutionResult→ModuleResult(role=EXECUTOR, work_item_status/batch_id/counters). gate unused phase 1 (arm keeps deterministic path).
  - Lazy-import delegates inside methods (dep-less).
- ADD `tests/orchestrator/agent_framework/test_sales_recovery_module.py`: assert_conforms + register + role-dispatch + adapter round-trip.
- EDIT ZERO existing SR files.
- Deferred cutover (separate Fazal-authorized money-path PR): (a) point supervisor._sales_recovery_node → module.propose; (b) coordinator.get_registry() → CoordinatorAgentAdapter for sales_recovery; (c) register_activation_prereqs live.

**BUILDER STOP-GUARDRAIL (addendum):** the migration touches NONE of `customer_send.py` / `approval_resume.py` / `collapse.py` / `approval_glue.py`. All 5 money boundaries + both send surfaces + the Gate 0..6 stack live OUTSIDE the module, byte-for-byte unchanged. The framework's `GateFacade.request_customer_send` EXISTS but the SR module does NOT call it in phases 1–5 (capability Open-Q #1). If a builder finds themselves editing any of those 4 files to do this migration, they've LEFT the thin-adapter path — STOP. Two send surfaces both stay module-external: Surface A executor→agent_draft_batches→`agent_customer_send`→agent_send_draft; Surface B proposer→collapse.py:150→campaigns→`campaign_send`→approval. Both ∈ approval_resume.py:73 `_CUSTOMER_SEND_APPROVAL_TYPES` — neither approval type authored by the module.

## 4. Invariant checklist (preserve; test that pins)
1 VT-651 cohort: test_sales_recovery.py:1860/1913/1942 · 2 VT-499 window :1151/1173/1230/1249 · 3 VT-498 scrub :1759/1796/1823 · 4 VT-501 refs :1293/1324/1351/1378/1400 · 5 coercion+identity :867/902/949/991 · 6 proposer AgentResult shape :704/:72 · 7 fail-loud missing bundle :737 · 8 executor consent gate test_sales_recovery_executor.py:628 · 9 arm-refusal cancels :650 · 10 persist-then-arm :535 · 11 detection 45d/limit :365/440 · 12 grounding drop :494 · 13 empty consent⇒0 :345/353 · 14 send Gate 0..5 test_customer_send.py:574/582/597/608/624/637/706/744 · 15 approval Pillar-7 (approval_resume.py:115/161) · 16 cross-tenant re-check supervisor.py:286 — NEW test · 17 coordinator registry adapter.name coordinator.py:151 — NEW test at cutover · 18 module conformance — NEW test via assert_conforms.

## 5. Ordered build steps (post-VT-657; money-path last)
0. Rebase on landed VT-657; confirm signatures of run_sales_recovery_agent, _construct_variant_payload, SalesRecoveryAgent.execute_item, validate_draft_params (only symbols the adapter binds).
1. Manifest + PROPOSER-only skeleton; assert_conforms + register test. Read-only.
2. AgentResult→ModuleResult adapter + round-trip test (proposer output byte-identical).
3. Add EXECUTOR role; ItemExecutionResult round-trip via CoordinatorAgentAdapter. Inert — coordinator registry untouched.
4. Activation-bar reuse test (exact existing AgentPrerequisites; register_activation_prereqs idempotent).
5. Conformance + dep-less-smoke + ruff; diff = 2 new files only.
6. DEFERRED Fazal-authorized cutover (separate money-path PR): repoint node/registry/prereqs → validate on deployed dev (j01/j06 win-back x3 + tier_rescore, cohort determinism + arm/approval unchanged) before promotion.

## 6. Collision map with VT-657
Phases 1–5 collision = NONE by construction (adapter ADDS files, binds VT-657 fns by name). Only rebase action: if VT-657 renames/re-signatures the 4 bound symbols, update the single delegation call. Land VT-657 first, then rebase. Shared-file touches (supervisor.py, coordinator.py) are cutover-only (step 6), untouched by VT-657.

## 7. OPEN — Fazal product/design calls (for the DEFERRED cutover, NOT blocking phases 1–5)
1. **Capability modeling of "arm ≠ send".** (A, RECOMMENDED + CC default) manifest declares only {READ_CUSTOMER_LEDGER, PROPOSE_CAMPAIGN} — truthful (manifest==behavior); executor arms via existing deterministic path; facade unused phase 1. (B) declare REQUEST_CUSTOMER_SEND now to match README = declared-but-unused = drift risk. (C, later) extend framework with gated REQUEST_ARM_APPROVAL cap routing to arm_agent_send_approval/enter_l3_hold, then full-port — only path making SR structurally facade-gated, but a money-path framework change, not launch scope. CC proceeds with A; C as tracked follow-on unless Fazal picks otherwise.
2. **Who runs the cutover + when** (Fazal who-does-it, mirrors VT-649/650). Phases 1–5 need no call.
3. **Retire duplicate proposer wrapper** `agent/sales_recovery_node.py` at cutover (dead divergence vs supervisor._sales_recovery_node).
