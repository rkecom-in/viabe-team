# Team-Manager Rebuild — Reuse Map (governs the build; no duplication, Fazal 2026-06-28)

## REUSE AS-IS (do NOT rebuild — call/extend these)
- `agent/orchestrator_agent.build_orchestrator_agent(model, *, extra_tools)` — Team-Manager node factory. VT-461 swaps the SYSTEM PROMPT (router→Business-Manager) + tool/handoff set; build seam unchanged (VT-194 caching + VT-268 guard already applied).
- `agent/supervisor.build_supervisor_graph(model, checkpointer)` — the supervisor-with-roster StateGraph (supervisor + route map + collapse→approval→execute send rail + PostgresSaver). Add roster NODES here; don't rewrite.
- `agent/handoffs.make_spawn_tool(...)` + `spawn_sales_recovery` — generic handoff factory; **SR handoff ALREADY wired (VT-463 mostly done)**. New specialist = 1 make_spawn_tool + 1 route branch + 1 node.
- `agent/routing.route_after_orchestrator/route_after_collapse/route_after_approval` — conditional-edge pattern; add a return key + path-map entry per roster member.
- `agent/orchestrator_agent_driver` hard limits (5 calls/10K tok/120s/₹5/depth3) — the cheap-single-call cost/latency rail (§1 "no fan-out on Hi").
- `agent/tool_guardrail.assert_agent_tools_safe` + FORBIDDEN_CAPABILITY_SUBSTRINGS — VT-460 capability rail (brain structurally holds NO send/write tool). Extend the forbidden-set, don't reinvent.
- `agents/customer_send.agent_send_draft` Gate-0 ladder — the deterministic non-bypassable send choke. Brain emits intent → routes through this unchanged.
- `agents/activation_registry.AgentPrerequisites` — the declarative prereq registry bounding VT-462; `onboarding/journey.py` keeps the onboarding_journey state.
- `agent/dispatch.dispatch_brain` — runner→supervisor seam (L1 context, observability, checkpointer, terminal class, compose-output exit). VT-461 changes the node inside, not this caller.
- `agent/tools/classify_owner_message` (Haiku classifier, typed envelope + CL-425/VT-270 consent gate) — REUSE; swap prompt v3→v4 for the label set. Do NOT build a parallel classifier.
- The 10-intent Classification Literal + `prompts/classify_owner_message_v3.md` — reuse the vocabulary + boundary examples; extend via a new version file.
- `pre_filter_gate.pre_filter` — deterministic Stage-1 (opt-out/DSR/L3-kill/status-ping/integration-intent). Supervisor sits DOWNSTREAM; keep these deterministic routes.
- `keyword_match` — reuse for any deterministic keyword route.

## GENUINELY NEW (the real build — nothing existing covers)
1. **VT-461** Team-Manager SYSTEM PROMPT (router→Business-Executioner/Team-Leader, bias-to-ACT per §6) — core net-new artifact.
2. **VT-461** framed intent-classification-as-primary + handle-directly-vs-delegate reasoning (reuses classify_owner_message; the reasoning-as-brain's-main-job is new).
3. **VT-462** onboarding-conductor sub-graph — dynamic brain-conducted onboarding bounded by activation_registry + journey state (reuses integration_agent's create_agent shape).
4. **VT-460** consolidated adversarial non-bypassability PROOF harness + the 4 send-path/transport gap-closes (see rail-harness-findings.md). Pieces exist; the consolidated proof + gap-closes are new.
5. **§6 AUTONOMY** machinery — owner POLICY granted at onboarding + autonomous-act-within-policy + escalate-only-EXTREME. Current = per-campaign owner approval (Pillar-7 per-send); escalate_to_fazal is a log-only stub. This is the biggest genuinely-new piece.
6. **VT-463** connect/integration handoff (SR already done).
7. **VT-464** new-brain live e2e re-drive (gates sign-off).

## (optional, design-implied) thin roster-registry so adding a specialist = a registry entry, not 4 edits across 3 files.
