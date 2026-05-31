---
vt_id: VT-4
title: VT-SalesRecovery-Agent — Anthropic Agent SDK, system prompt, hard limits
status: Done
priority: Critical
sprint: Sprint 2 - SR Agent Skeleton
type: Feature
area: [Specialist Agent]
assignee: Clau
parent: ""
sub_items: [VT-32, VT-33, VT-34, VT-35, VT-36, VT-37, VT-38, VT-135, VT-136, VT-137, VT-138, VT-139, VT-140, VT-141, VT-163, VT-164, VT-165]
exec_order: 1
branch: "feat/vt-sales-recovery-agent"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-816c-a2cf-de50a43fda45
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-4 — VT-SalesRecovery-Agent — Anthropic Agent SDK, system prompt, hard limits

## Why this parent exists
The Sales Recovery Agent is the only Tier 2 specialist in Phase 1. It is where domain reasoning happens — what messages to send to which dormant customers, when, and why. Tier separation only works if reasoning is concentrated here and tools never reason. Reports product violated this multiple times: LLM-backed rent estimators, mid-pipeline calculations dressed as data fetches, prompt enrichment that snuck business logic into producers. That is why Pillar 2 exists: reasoning lives in the agent, not in the tool.
The Phase 1 agent is built clean: Anthropic Agent SDK, a tight system prompt, structured output schema, hard limits enforced at the orchestrator boundary, and a self-evaluate gate before output ships. No free-form drafts. No silent fallbacks. No cross-domain drift. The system prompt is small, the output is structured, and the agent's scope is narrow on purpose.

## What this parent owns
1. Anthropic Agent SDK skeleton wrapped as a LangGraph node consumed by the orchestrator's dispatch step (VT-3.4).
2. System prompt v1.0 covering identity, scope, available tools, output schema, refusal patterns, hard-limit awareness. Capped at 4,000 tokens; if the prompt grows past that, content moves into the L4 skill corpus (VT-7.7) instead.
3. Context bundle contract (`SalesRecoveryContext`) — the typed shape the orchestrator passes the agent at invocation. Includes subscriber tenant scope, customer ledger summary cursor, recent campaign history, attribution snapshot, and trigger reason.
4. Hard-limit enforcement: orchestrator measures, agent terminates with `escalate_to_owner` when it exceeds 80K tokens, 25 tool calls, depth 8, or 5-minute wall clock. Termination is non-negotiable; the agent does not get to decide it has special permission to continue.
5. Self-evaluate step that grades the agent's own draft before returning, with a defined retry path. Uses Opus model. Gates the structured output.
6. Structured output schema (`CampaignPlan`) the agent must conform to: campaign window, target customer cohort, message template ID + parameters, expected attribution close date, exclusion list, escalation conditions.
7. Scope-discipline tests that catch the agent drifting beyond Sales Recovery into Reputation, Marketing, or Operations.

## Architectural rules binding every subtask
- Pillar 1 (tier separation): the agent reasons about subscriber data; it does not write to the database directly, does not send messages directly, does not fetch data directly. All side effects route through MCP tools (VT-5).
- Pillar 2 (reasoning in agent, not tool): the agent makes the campaign decisions. Tools return data and execute deterministic actions. If a tool starts reasoning, the design has drifted — fix the tool, do not push reasoning into it.
- Pillar 4 (retrieve, don't calculate): the agent never invents footfall, churn rates, or category benchmarks from a Python dict. It retrieves from L1-L4 knowledge or the customer ledger (VT-5.2).
- The system prompt cannot exceed 4,000 tokens. If it grows past that, refactor into the L4 skill corpus.
- The agent never has tenant-spanning context. Every invocation is scoped to one subscriber, enforced by the context bundle contract.
- The agent must never bypass `self_evaluate`. Output is gated, period.

## Subtasks under this parent
1. **VT-4.1** — Anthropic Agent SDK skeleton wired into LangGraph as a node.
2. **VT-4.2** — System prompt v1.0 (identity, scope, tools, output schema, refusals).
3. **VT-4.3** — `SalesRecoveryContext` bundle contract.
4. **VT-4.4** — Hard-limit enforcement at the orchestrator/agent boundary.
5. **VT-4.5** — Self-evaluate step (Opus-backed, gates final output).
6. **VT-4.6** — Structured output schema (`CampaignPlan`); no free-form drafts ship.
7. **VT-4.7** — Scope-discipline tests catching cross-domain drift.

## Definition of done
- All 7 subtasks Done.
- Canary run on a synthetic subscriber produces a valid `CampaignPlan`, conforms to the output schema, and passes self-evaluate without retry.
- Scope-discipline tests fail loudly when the agent is prompted with reputation, marketing, or unrelated domain inputs.
- Hard-limit enforcement test confirms termination at 25 tool calls, 80K tokens, depth 8, and 5-minute wall clock — each independently tested.
- System prompt v1.0 token count measured and recorded; under 4,000.
- Fazal personally reviews and signs off on system prompt v1.0.

## Out of scope
- MCP tools the agent calls (VT-5).
- L1-L4 knowledge architecture (VT-7).
- Phase 2 specialists (Reputation, Marketing) — out of Phase 1 entirely.
- Owner-facing message composition — that is the orchestrator's unified-output composer (VT-3.7), not the agent's job.
- Billing reasoning, refund decision logic — those are deterministic SQL (VT-10.4), not agent decisions.

## Branch convention
- Parent branch: `feat/vt-sales-recovery-agent`.
- Subtask branches: `feat/vt-sr-agent-<short>` (e.g. `feat/vt-sr-agent-system-prompt`).
- PR title format: `<type>(sr-agent): <description> (VT-4.N)`.
- Reviewers: CoderC, then CoderX. Fazal reviews system prompt v1.0 personally before merge.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-816c-a2cf-de50a43fda45)
