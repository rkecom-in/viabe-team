---
vt_id: VT-7
title: VT-KnowledgeArchitecture — 4-layer KG/episodic/Layer-3/skill corpus
status: Done
priority: Critical
sprint: Sprint 7 - Knowledge Architecture
type: Feature
area: [Knowledge Architecture, Database]
assignee: Clau
parent: ""
sub_items: [VT-64, VT-65, VT-66, VT-67, VT-68, VT-69, VT-70, VT-71, VT-124, VT-142, VT-143, VT-146, VT-155, VT-159]
exec_order: 1
branch: "feat/vt-knowledge"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-8108-8540-efd4a78e9188
last_updated: 2026-06-04T06:10:00Z
---

# VT-7 — VT-KnowledgeArchitecture — 4-layer KG/episodic/Layer-3/skill corpus

## PHASE-1 BUILD COMPLETE 2026-06-04 — VT-7 → Done
The 4-layer knowledge architecture is built + verified on main: **L1** entities/relationships (voyage
vector(1024)+HNSW, hash-phone), **L2** episodic_events + kg_events dual-projection (VT-66/67/309), **L3**
cross-tenant priors (n_tenants≥10 CHECK, VT-68/69), **L4** skills corpus (voyage-4-lite, VT-70), single
**composition** (Pillar-8 build_sales_recovery_context, VT-71) — each canaried on the live composition path;
adversarially verified sound. Owner_inputs (VT-146/155/159) + reconciliation rows Done.

**Residual children — NOT Phase-1-build, tracked separately (none is an open CC code row):**
- **VT-312** (Blocked) — detector thresholds → Fazal-gated (gate-live; reconstitution coverage grows with it).
- **VT-311** (Backlog) — L2 18-month retention + 100K-event perf → Phase-2.
- **VT-316** (Backlog) — pre-push hook fail-loud → rostered follow-up (CI-hygiene).
Plus the Fazal-gated corpus/key launch-prereqs (VT-313 L4 corpus, VT-314 voyage billing) tracked in the launch checklist.

**LOUD go-live-prereq tracker: [`.viabe/customer-data-go-live-prereqs.md`](../customer-data-go-live-prereqs.md).**

## Why this parent exists
The agent reasons; tools execute; what does it reason over? The 4-layer knowledge architecture is the answer. L1 is per-subscriber business facts (a structured graph in Apache AGE). L2 is per-subscriber episodic memory of every campaign and message. L3 is the cross-subscriber learning layer — patterns built from many subscribers' outcomes, but only over groups of 10+ to satisfy k-anonymity (Pillar 6). L4 is hand-authored skill corpus on Sales Recovery domain knowledge.
This parent is the schedule risk. K-anon discipline cannot be cut, and L3 is where that discipline either holds or collapses. Reports product had no equivalent of L3 — its knowledge graph was per-locality only. Team's L3 introduces a structurally different problem: information must flow across tenants without leaking tenant-specific data. The wrong design here turns into a privacy incident.

## What this parent owns
1. Apache AGE setup on the Supabase Postgres instances (dev and prod). Migrations create the AGE extension and the per-subscriber graph schema.
2. L1 KG (knowledge graph) population pipeline: business facts written into a per-subscriber subgraph.
3. L2 episodic schema and write path: every campaign, owner message, and attribution outcome embedded via pgvector.
4. L2 retrieval contract: how the agent queries episodic memory through MCP tools.
5. L3 pattern construction with k-anon coarsening at construction time. Patterns built only over groups of 10+, with locality coarsened to ward (urban) or city-tier (rural).
6. L3 retrieval with 180-day pattern quarantine: newly-built patterns are not visible to the agent for 180 days, allowing time to detect false patterns.
7. L4 skill corpus seed: ≥30 hand-authored markdown documents on Sales Recovery domain knowledge (re-engagement message patterns, attribution windows, exclusion rules, etc.). Embedded via pgvector for retrieval.
8. Composition layer: combines L1-L4 retrieval into the agent's context bundle. Audit-logged: every retrieval logs which layers contributed.

## Architectural rules binding every subtask
- Pillar 6 (k-anon is build-time, not runtime): L3 patterns are constructed only over groups of 10+. Coarsening to ward (urban) or city-tier (rural) happens at construction. Never write per-individual data to L3 and check k afterwards. Verify the invariant in every L3 construction commit via a CI test.
- Pillar 5 (no fine-tuning): improvements happen via prompts and retrieval. The L4 skill corpus is the lever for adding domain knowledge without modifying agent behavior.
- Pillar 3 (tenant isolation): L1 and L2 are per-subscriber, scoped by `tenant_id`, and read only via typed wrappers. L3 is cross-tenant by construction (it is the only layer that crosses tenants), and exactly because of that it cannot contain raw tenant data — only aggregates that satisfy k-anon.
- Pillar 8 (no patchwork): when a retrieval returns suspicious results (e.g., L3 pattern that looks like a tenant-specific leak), the response is to fix the construction logic and rebuild — not to filter the result post-hoc.
- Audit logging: every retrieval call writes a row to `knowledge_retrieval_log` with run_id, tenant_id, layers consulted, query embeddings hash, results count, latency.
- The 180-day quarantine on L3 patterns is non-negotiable. New patterns are flagged `quarantined_until` and excluded from retrieval until that date.
- The L4 skill corpus is hand-authored and version-controlled in the repo at `apps/team/skill_corpus/`. It is NOT generated by an LLM. Edits go through PR review.

## Subtasks under this parent
1. **VT-7.1** — Apache AGE setup and KG schema (per-subscriber).
2. **VT-7.2** — L1 KG population pipeline.
3. **VT-7.3** — L2 episodic schema and write path.
4. **VT-7.4** — L2 retrieval contract.
5. **VT-7.5** — L3 pattern construction with k-anon coarsening at construction.
6. **VT-7.6** — L3 retrieval with 180-day quarantine.
7. **VT-7.7** — L4 skill corpus seed (≥30 docs).
8. **VT-7.8** — Composition layer with audit logging.

## Definition of done
- All 8 subtasks Done.
- Apache AGE running on dev and prod Supabase. Per-subscriber subgraphs created and queryable.
- L1 population from a fully-onboarded subscriber: business facts represented as a graph, queryable in <100ms.
- L2 retrieval returns the most relevant episodic memory for a given query within 200ms.
- L3 invariant test passes: a synthetic dataset with 9 subscribers in a ward produces zero L3 patterns (k=10 floor enforced). Adding a 10th produces the pattern. Coarsening to city-tier behaves identically for rural localities.
- L3 quarantine test passes: a newly-constructed pattern is invisible to retrieval until 180 days have elapsed (simulated via test-clock).
- L4 corpus has ≥30 documents, all reviewed by Fazal personally before commit.
- Composition layer audit log captures which layers contributed to a synthetic agent run.
- Cross-tenant attack test on L1 and L2: tenant A cannot retrieve tenant B's data via any composition path.

## Out of scope
- The MCP tools that consume retrieval (VT-5).
- The agent that uses the composed context (VT-4).
- Privacy enforcement layers — typed wrappers (VT-8.1), agent context isolation (VT-8.2), DSR APIs (VT-8.6) — even though they touch the same tables.
- The orchestrator's scheduled trigger that recomputes L3 patterns nightly — that is a VT-3.5 concern.
- Real-time pattern construction — Phase 1 builds L3 patterns nightly via batch job, not on every write.

## Branch convention
- Parent branch: `feat/vt-knowledge`.
- Subtask branches: `feat/vt-knowledge-<short>` (e.g. `feat/vt-knowledge-l3-construction`, `feat/vt-knowledge-skill-corpus`).
- PR title format: `<type>(knowledge): <description> (VT-7.N)`.
- Reviewer: Fazal (mandatory CODEOWNERS sign-off). Fazal personally reviews every L3 construction PR for k-anon invariant. Fazal personally reviews L4 corpus.
- Merge target: `main` (CL-2026-05-16 branch strategy: main = prod, no dev branch).

## 2026-05-16 ARCHITECTURE REVISION (audit-driven, supersedes original spec above)
**This entire parent's L1 architecture changed on 2026-05-16. Read this section before reading anything above it.**

### What changed
Apache AGE is NOT supported on Supabase (verified via Supabase discussion #40285, Nov 2025; Nix-sandboxed Postgres images prevent extension compilation). The original spec encoded a 2024-era assumption that Supabase would support AGE. It doesn't, and the Supabase team has no roadmap to add it.

### New L1-L3 architecture: Mem0 OSS library + custom L0/L4

> **⚠️ SUPERSEDED (VT-162 board-hygiene, 2026-06-06).** The Mem0-based design described in this
> section was NOT shipped. The knowledge layers are **hand-built on Postgres + pgvector +
> time-aware relational schema** (no Mem0, no Apache AGE — see `l1-knowledge-graph-no-age`). The
> live L1/L2/L3 lives in `orchestrator/knowledge/` (e.g. `l1.py`, the `l1_*`/`l2_*`/`l3_*`
> migrations + the k≥10 admission gate), NOT `mem0ai`. The text below is retained for design
> history only — do not treat it as the implemented architecture.

**Mem0 OSS** (`pip install mem0ai`, Apache 2.0 license) is the production-standard pattern in 2026 for exactly the use case this parent was designed for. Mem0 v1.0.0 implements:
- Postgres + pgvector + entity linking (dual-store)
- Multi-tenant scoping via `user_id`, `agent_id`, `run_id`, `app_id` filters with AND/OR composition
- Multi-signal hybrid retrieval (vector similarity + BM25 + entity matching)
- MCP server integration for Claude Code

**Architecture clarification (critical — do not confuse these):**
- Mem0 OSS = the FREE Python library. Runs IN our team-orchestrator process on Railway. Writes to OUR existing Supabase Cloud Postgres database (no separate infrastructure).
- Mem0 Cloud = paid managed service. NOT what we're using.
- The word "self-hosted" in Mem0's docs means "you provide the Postgres, we provide the library" — NOT "run your own Postgres server."

### Split of responsibility (NEW)
**L1 — tenant business knowledge** → **Mem0** with `user_id=tenant_id`, `app_id='l1_business'`. Replaces the original Apache AGE per-subscriber subgraph approach.
**L2 — customer profiles** → **Mem0** with `user_id=tenant_id`, `app_id='l2_customer'`, `metadata.customer_id`. Replaces the original pgvector-only episodic schema.
**L3 — anonymized cross-tenant patterns** → **Mem0** with `app_id='l3_anonymized'` (no user_id scoping). The k≥10 enforcement at admission gate stays custom (Pillar 6 — build-time invariant). Mem0 stores the patterns; our admission gate decides which patterns qualify.
**L0 — workspace knowledge for Viabe-specific orchestration patterns** → **STAYS CUSTOM**. Admission gate logic, k≥10 enforcement, escalation rules, terminal states. This is specific to Viabe's business, not memory-as-substrate.
**L4 — hand-authored skill corpus + closed-loop feedback** → **STAYS CUSTOM**. ≥30 hand-authored markdown documents on Sales Recovery domain knowledge. Owner-approval feedback unique to our owner-in-the-loop pattern.

### Why this is better
1. Works on Supabase TODAY (no infrastructure switch)
2. Adds time-aware data hooks (Mem0 metadata supports `valid_from` / `valid_to` even if not first-class) that Apache AGE didn't give us anyway
3. Aligns with 2026 production pattern (Mem0 v1.0.0 explicitly replaced external graph store support with built-in entity linking)
4. Single DB = single backup surface, single RLS surface, simpler operations
5. ~3 days of integration replaces ~3 weeks of custom L1 build — saves 1-2 weeks of Sprint 7 time

### What we lose (accepted trade-offs)
- Cypher query language (we use SQL through Mem0 client)
- Some 3+ hop graph traversals will be slower (recursive CTEs vs AGE native graph engine). Not on Phase 1-3 critical path.
- Mem0 owns its core schema. We can extend with columns (Postgres permits) but if Mem0 changes schema in a future version, we migrate with them.

### Subtask respec required (Sprint 7 trigger work)
**VT-7.1 — Apache AGE setup and KG schema (per-subscriber)** → RESPEC to: "Mem0 OSS library integration. Add `mem0ai` to apps/team-orchestrator/pyproject.toml. Configure Mem0 with TEAM_SUPABASE_URL + Anthropic Claude for entity extraction (use Claude Haiku to minimize cost). New migration in /migrations/ creating Mem0's schema tables (memories, entity collection, relationships) in our Supabase Postgres. Apply RLS policies to Mem0's tables in the SAME migration (Pillar 3 inline-RLS rule). Verify multi-tenant isolation via cross-tenant attack test."
**VT-7.2 — L1 KG population pipeline** → RESPEC to: "Tenant business knowledge ingestion into Mem0 with user_id=tenant_id, app_id='l1_business'. Onboarding flow writes business facts (segments, products, rules) via mem0.add() calls. Entity extraction happens automatically via Mem0's Claude integration. Retrieval contract: [mem0.search](http://mem0.search)() with tenant scoping filter."
**VT-7.3 to VT-7.6** — L2 + L3 schema and retrieval contracts continue to apply but adapt to Mem0's API surface instead of direct pgvector queries. Specifics in each subtask brief when reached.
**VT-7.7 — L4 skill corpus seed** → NO CHANGE (custom build, hand-authored).
**VT-7.8 — Composition layer** → RESPEC: combine L0 (custom) + Mem0 L1-L3 retrieval + L4 (custom). Audit-log which layers contributed.
**New VT-7.9 (add to subtask list) — L0 workspace memory (custom)** → Already exists as separate VT-7 subtask page "L0 workspace memory write path + retrieval (orchestrator-only)" at <mention-page url="https://www.notion.so/360387c2cc5a811cb3cded15c45cf352"/>. Stays custom.

### Reference
Full rationale in Clau_Session_Log CL-Mem0-decision (2026-05-16) + CL-L1-revision (2026-05-16). See also Viabe Team Stack Audit — May 2026 page in resurrection file for the full audit context.

### IMPORTANT TERMINOLOGY
"Time-aware" / "validity-window" in the L1 context = data model property (facts have valid_from/valid_to). This is NOT related to Temporal-the-product (the workflow orchestration tool rejected per CL-27 in favor of DBOS). DBOS remains the durable execution substrate. The collision of the word "temporal" between these contexts is a documentation hazard; use "time-aware" or "validity-window" going forward.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-8108-8540-efd4a78e9188)
