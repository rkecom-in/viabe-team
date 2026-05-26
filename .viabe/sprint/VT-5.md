---
vt_id: VT-5
title: VT-MCP-Tools — 11 tools + framework + harness
status: Backlog
priority: Critical
sprint: Sprint 2 - SR Agent Skeleton
type: Feature
area: [MCP Tools]
assignee: Clau
parent: ""
sub_items: [VT-39, VT-40, VT-41, VT-42, VT-43, VT-44, VT-45, VT-46, VT-47, VT-48, VT-49, VT-50, VT-51]
exec_order: 2
branch: "feat/vt-mcp-tools"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-819c-a8a1-c8a8600846c0
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-5 — VT-MCP-Tools — 11 tools + framework + harness

## Why this parent exists
MCP tools are Tier 3 — deterministic execution. The agent (Tier 2) decides; tools execute. Pillar 2 makes this absolute: only three of the eleven tools at v1 may be LLM-backed (`self_evaluate`, `classify_owner_message`, and the optional `draft_message_variants`), and each requires explicit justification in the spec. Reports product's pipeline blurred this constantly — half its tools called LLMs to make decisions a deterministic function should have made. Team locks the boundary in: tool framework + harness first, then individual tools, with a contract every tool must satisfy.
The framework is built before any individual tool. This is deliberate. If individual tools land before the framework, they each invent their own conventions and the framework becomes retroactive. Reports' tool layer suffered exactly this. Team builds the harness, the contract, and the test fixtures first, then implements tools to a single shared shape.

## What this parent owns
1. The MCP tool-contract framework: input schema, output schema, retry behavior, timeout, telemetry hooks, error envelope. Lives in `packages/team-shared/mcp/`.
2. A test harness for tool behavior verification: every tool ships with deterministic test fixtures and at least one negative-path test.
3. The 11 tools at v1, individually:
	- `query_customer_ledger` (paginated, segmented, RLS-scoped)
	- `get_business_profile` (returns subscriber identity facts from L1 KG)
	- `get_recent_campaigns` (last N campaigns with status and attribution)
	- `get_attribution_data` (attribution snapshot for a campaign window)
	- `send_whatsapp_message` (free-form, 24-hour window only, Twilio)
	- `send_whatsapp_template` (Meta-approved templates, no time restriction)
	- `match_transactions` (UPI string matching against ledger, confidence-scored)
	- `request_owner_approval` (session-pause; persists state; resumes on owner reply)
	- `schedule_followup` (writes to orchestrator scheduled-trigger table)
	- `classify_owner_message` (LLM-backed; routes inbound messages)
	- `self_evaluate` (Opus-backed; gates agent output)
4. Optionally `draft_message_variants` (LLM-backed), decision locked in VT-5.1 framework subtask.

## Architectural rules binding every subtask
- Pillar 2 (reasoning in agent, not tool): tools return structured data or execute deterministic actions. Only `self_evaluate`, `classify_owner_message`, and the optional `draft_message_variants` may invoke an LLM. Each LLM-backed tool's spec must explain why an LLM is the right call instead of a deterministic function.
- Pillar 3 (tenant isolation): every tool reads from Postgres through the typed wrapper layer (VT-8.1). RLS policies enforce isolation at the database level. A tool cannot accept a `tenant_id` parameter from the agent — it derives it from invocation context, which the orchestrator stamps.
- Pillar 4 (retrieve, don't calculate): tools that surface signals (footfall, churn, persona traits) retrieve from L1-L4 or the source data. No business-type-specific dicts inside any tool.
- Pillar 8 (no patchwork): tool errors return structured errors with diagnostic codes via the framework's error envelope. No regex scrubs, no value clamps to fix upstream data, no last-minute string replacements.
- Every tool has a deterministic test fixture and at least one negative-path test (wrong tenant, malformed input, dependency failure).
- Every tool's call is logged to `pipeline_log` with run_id, tenant_id, tool_name, inputs hash, output hash, latency, error_code if any.

## Subtasks under this parent
1. **VT-5.1** — Tool-contract framework + harness. Decides whether `draft_message_variants` is in v1.
2. **VT-5.2** — `query_customer_ledger`.
3. **VT-5.3** — `get_business_profile`.
4. **VT-5.4** — `get_recent_campaigns`.
5. **VT-5.5** — `get_attribution_data`.
6. **VT-5.6** — `send_whatsapp_message`.
7. **VT-5.7** — `send_whatsapp_template`.
8. **VT-5.8** — `match_transactions`.
9. **VT-5.9** — `request_owner_approval`.
10. **VT-5.10** — `schedule_followup`.
11. **VT-5.11** — `classify_owner_message` (LLM-backed; rationale in spec).
12. **VT-5.12** — `self_evaluate` (Opus-backed; rationale in spec).
13. **VT-5.13** — `draft_message_variants` (LLM-backed; optional, scope locked in 5.1).

## Definition of done
- All applicable subtasks Done. (5.13 may end up Deferred per Pillar 2 review in 5.1.)
- 100% of tools satisfy the contract framework. Harness tests pass.
- A canary agent run uses 4-5 tools end-to-end and returns a valid `CampaignPlan`.
- Tools that touch tenant data verifiably fail when invoked with a wrong tenant context (proven by a cross-tenant attack test).
- Every LLM-backed tool's spec contains a written rationale for why an LLM is required.
- Telemetry: all tool calls visible in `pipeline_log`, queryable by run_id and tenant_id.

## Out of scope
- The Sales Recovery Agent itself (VT-4).
- The LangGraph orchestrator (VT-3).
- Apache AGE / pgvector knowledge stores the tools query (VT-7).
- Twilio approvals and Meta WhatsApp template approvals — those are infrastructure (VT-13.3, VT-13.6).
- Owner-facing UX (VT-9).

## Branch convention
- Parent branch: `feat/vt-mcp-tools`.
- Subtask branches: `feat/vt-mcp-<tool-short>` (e.g. `feat/vt-mcp-query-customer-ledger`).
- PR title format: `<type>(mcp-tools): <description> (VT-5.N)`.
- Reviewers: CoderC implementation; CoderX must review every tool that touches tenant data and every LLM-backed tool's rationale.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-819c-a8a1-c8a8600846c0)
