---
vt_id: VT-3
title: VT-Orchestrator — LangGraph, dispatch, triggers, error handling
status: Backlog
priority: Critical
sprint: Sprint 1 - Foundation
type: Feature
area: [Orchestrator, Infrastructure]
assignee: Clau
parent: ""
sub_items: [VT-24, VT-25, VT-26, VT-27, VT-28, VT-29, VT-30, VT-31, VT-125, VT-126, VT-127, VT-128, VT-129, VT-130, VT-131, VT-132, VT-133, VT-134]
exec_order: 2
branch: "feat/vt-orchestrator"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-814d-a104-fb4c186f7e22
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-3 — VT-Orchestrator — LangGraph, dispatch, triggers, error handling

## Why this parent exists
**Architectural evolution 2026-05-12: orchestrator-as-agent (Type 2 change, locked by Fazal: 'The Orchestrator has to be the Claude Code equivalent agent and that is final.')**. Supersedes the prior thin-router framing. Concept doc Section 8.1's 'orchestrator implements no business logic' is reinterpreted as 'no Sales-Recovery-specific logic' — not 'no reasoning'. The orchestrator now reasons about coordination and routing; specialists reason about domain work.
The orchestrator is the load-bearing top tier of the three-tier architecture. Reports product had no orchestrator equivalent — its CrewAI Factory was both router and reasoner, which is why bug fixes kept landing in the wrong layer. Team must enforce tier separation from day one.
**Architecture:**
- Orchestrator-Agent (Opus 4.7) runs as an agent with its own context bundle and L0 memory (workspace-wide, separate from tenant-scoped L1-L4).
- LangGraph is the SUBSTRATE the orchestrator-agent runs on (not the orchestrator itself).
- DBOS wraps the entire workflow (durable execution from Day 1; auto-resume on crash; supersedes 'checkpointer-only' framing).
- `langgraph_supervisor` library handles specialist spawning via auto-generated handoff tools.
- Two-stage filtering: (1) cheap deterministic pre-filter (regex/signature checks for opt-outs, DSRs, status pings, dupes) handles ~70% of events without invoking the brain; (2) orchestrator brain (Opus 4.7) handles remaining ~30%.
- Orchestrator decides per event: spawn specialist via `spawn_<specialist>` handoff tool / run deterministic pipeline / respond directly / listen-and-remember (write to L0 only).
This parent defines the seam where the orchestrator-agent hands off to specialist agents (currently only Sales Recovery in Phase 1; Phase 2-6 add 6 more). That seam is where hard limits are enforced.

## What this parent owns
1. DBOS workflow wrapping for the orchestrator service (every pipeline run is a `@DBOS.workflow`; every step is a `@DBOS.step`).
2. LangGraph substrate + Postgres checkpointer wired to Supabase (DBOS uses the same Postgres for its own `dbos_*` tables).
3. The deterministic pre-filter (Stage 1) — regex/signature checks, idempotency lock, direct handlers for routine routing.
4. The orchestrator-agent runtime (Stage 2) — Opus 4.7 with its own context bundle assembled by the Context Composer from L0 (workspace) + tenant L1-L4.
5. `langgraph_supervisor` integration with `create_handoff_tool()` generating `spawn_sales_recovery` (Phase 1) and additional `spawn_<specialist>` tools (Phase 2-6).
6. The `SubscriberState` schema and phase transitions: onboarding → trial → trial_extended → paid_active → paid_at_risk → cancelled → refunded.
7. Twilio inbound webhook — routes through pre-filter then orchestrator-agent.
8. Four scheduled triggers (DBOS scheduled workflows): weekly cadence, attribution close T+7, day-39 evaluation, monthly impact report.
9. Error-handling and retry framework with taxonomy. DBOS handles auto-resume for system errors; this layer handles business-logic errors (revise loop, hard-limit termination, escalation to Fazal).
10. Unified-output composer that merges agent response + orchestrator status into a single owner-facing message.
11. L0 memory write path (orchestrator-only) through k-anonymity gate.

## Architectural rules binding every subtask
- **Pillar 1 (tier separation):** the orchestrator-agent reasons about coordination and routing only. Domain reasoning lives in specialists. CodeX rejects PRs where the orchestrator-agent makes Sales-Recovery-specific decisions.
- **Pillar 8 (no patchwork):** error handlers classify against the taxonomy and route to a strategy. No silent retries, no exception-swallowing. New failure modes → taxonomy first, handler second.
- **Hard limits per agent invocation ([concept-team.md](http://concept-team.md) §8.3):** 80,000 tokens, 25 tool calls, 8 reasoning depth, 5-minute wall clock, ₹50 cost. Orchestrator-agent enforces at the dispatch boundary; terminates with `escalate_to_owner` step record.
- **DBOS step boundaries = observability step boundaries.** Every `@DBOS.step` is also a `@observability.step`. Atomic write to both `dbos_workflow_steps` and `pipeline_steps` in the same transaction.
- **DBOS auto-resume** on Railway crash. No 'failed' terminal state for system errors; only `completed`, `escalated`, `aborted_hard_limit`, `duplicate_rejected`.
- **Supervisor pattern** via `langgraph_supervisor` library (Context7 ID `/langchain-ai/langgraph-supervisor-py`). NOT custom routing. Adding a Phase 2-6 specialist = register with supervisor + add handoff tool, no routing rewrite.
- **Two-stage filter:** Stage 1 deterministic catches ~70% of events at ₹0 cost. Stage 2 brain (Opus 4.7) only on ~30% requiring judgment.
- **L0 writes through k-anonymity gate.** Only orchestrator-agent writes to L0. Specialists never touch L0.
- Every state transition writes one row to `pipeline_steps` via the `@step` decorator.
- The orchestrator-agent never holds long-lived secrets. Reads from env on each invocation.
- All scheduled triggers idempotent: re-running for same subscriber+window is a no-op (DBOS idempotency keys).

## Subtasks under this parent (respec required)
Existing 8 subtasks (VT-3.1 through VT-3.8) need rewriting to reflect orchestrator-as-agent + DBOS + supervisor. Specifically:
1. **VT-3.1** — LangGraph skeleton **wrapped in DBOS workflow** with Postgres checkpointer (becomes 'orchestrator-agent runtime ON LangGraph substrate' rather than 'LangGraph state machine').
2. **VT-3.2** — `SubscriberState` schema and phase transitions. Shifts from owning routing logic to providing the orchestrator-agent's memory hooks.
3. **VT-3.3** — Twilio inbound webhook + idempotency check + DBOS workflow start (becomes pure Stage 1 entry point; LLM classifier moves out).
4. **VT-3.4** — Was 'Specialist dispatch node'; becomes the **Supervisor integration** with `langgraph_supervisor` + `spawn_sales_recovery` handoff tool wiring.
5. **VT-3.5** — 4 scheduled triggers as DBOS scheduled workflows (DBOS handles cron + idempotency natively).
6. **VT-3.6** — Error-handling and retry framework with taxonomy; DBOS auto-resume for system errors layered underneath.
7. **VT-3.7** — Unified-output composer. Becomes one of the orchestrator-agent's tools.
8. **VT-3.8** — Was 'Multi-specialist hooks'; becomes **Pre-Filter Gate (Stage 1)** — regex/signature checks + direct handlers. Multi-specialist readiness now natively handled by supervisor.
**New subtasks needed (additive):**
1. **VT-3.9** — Orchestrator-Agent system prompt + tool inventory (own context bundle setup; spawn_<specialist> tools registered).
2. **VT-3.10** — L0 memory write path through k-anonymity gate (orchestrator-only).
Subtask body rewrites are tracked in Clau_Session_Log entries CL-22 (orchestrator-as-agent), CL-23 (two-stage filter), CL-24 (L0 memory), CL-25 (subtask respec required), CL-27 (DBOS chosen).

## Definition of done
- All 10 subtasks Done (8 respec + 2 new).
- DBOS workflow wraps orchestrator service; smoke test confirms auto-resume on synthetic crash.
- Smoke run completes end-to-end: synthetic owner message → webhook → idempotency check → pre-filter → orchestrator-agent reasoning → supervisor handoff to stub Sales Recovery agent → unified output → phase transition logged in `pipeline_steps`.
- Hard-limit enforcement verified: test that exceeds 25 tool calls confirms `escalate_to_owner`.
- Two-stage filter verified: synthetic opt-out message hits direct handler at ₹0 LLM cost.
- Idempotency tests pass for each of 4 scheduled triggers (DBOS guarantees + business-logic guarantees).
- L0 write through k-anonymity gate: tenant-identifying fragment rejected at gate; aggregate fragment admitted.
- Phase transitions exactly match [concept-team.md](http://concept-team.md) §8.
- Error taxonomy v1 documented at `docs/team/error-taxonomy.md`.

## Out of scope
- The Sales Recovery Agent itself (VT-4).
- MCP tools the agent calls (VT-5).
- Owner-facing UX surfaces — live in VT-9 (Owner Surface).
- Billing-related state transitions — live in VT-10.
- Knowledge architecture (L1-L4) — that's VT-7. The orchestrator-agent reads from it via Context Composer but doesn't define schema here.
- Pipeline Observability tables — those are VT-122. This parent uses the `@step` decorator; doesn't own the writer.

## Branch convention
- Parent branch: `feat/vt-orchestrator`.
- Subtask branches: `feat/vt-orchestrator-<short>` (e.g. `feat/vt-orchestrator-dbos-wrap`, `feat/vt-orchestrator-supervisor`).
- PR title format: `<type>(orchestrator): <description> (VT-3.N)`.
- Reviewers: CoderC for implementation; CoderX must review the DBOS workflow boundaries, the supervisor handoff wiring, the two-stage filter cost split, the L0 write path k-anonymity enforcement.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-814d-a104-fb4c186f7e22)
