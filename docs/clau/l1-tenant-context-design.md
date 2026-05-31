# L1 Tenant Context — Design (Option A: integrate over the existing L1 KG)

> **Status: REFRAMED to Option A (Cowork ruling 2026-05-31; Fazal veto pending on
> Option B).** The earlier version of this doc designed a NEW flat
> `tenant_l1_profile` table — that was a duplicate-substrate mistake. The L1
> knowledge graph ALREADY EXISTS: `migrations/019_l1_knowledge_graph.sql`
> (l1_entities + l1_relationships, pgvector HNSW, FORCE RLS) = the VT-7.1 work,
> with the read API in `orchestrator/knowledge/l1.py` (search_entities,
> traverse_relationships). VT-195's real gap is the **Context Composer
> integration** over that substrate, NOT a new table. `tenant_l1_profile` is
> abandoned; D1 (flat attributes) and D3 (flat write path) are MOOT. See
> `.viabe/queue/VT-195/plan.md`.

## 0. Purpose
L1 = the **per-tenant knowledge substrate** the orchestrator-agent reads so it
behaves as *this* business's agent (Fazal's moat directive, 2026-05-27). L0
(VT-126) = workspace-wide cohort priors; L1 = tenant-scoped. L2–L4 = Mem0,
deferred (CL-324). Hand-built Postgres + pgvector (no Apache AGE — unsupported on
Supabase).

## 1. Substrate — the EXISTING entity graph (no new table)
`l1_entities (id, tenant_id, entity_type, attributes JSONB, embedding
vector(1024) NULL, valid_from, valid_to, created_at)` + `l1_relationships`
(from/to entity, recursive-CTE traversal). FORCE RLS, `tenant_id =
app_current_tenant()` (mig 019). Read API: `search_entities(tenant_id, *,
entity_type, attributes_filter, text_query, query_embedding, limit)` and
`traverse_relationships(...)`.

A tenant's durable **identity** is one entity: `entity_type = 'business_profile'`,
`attributes` = { business_archetype, owner_persona, integration_map,
working_hours, escalation_thresholds, communication_prefs, owner_curated_context }.
(Same fields the abandoned flat design chose — but as an entity's attributes.)
The graph also holds richer per-tenant facts (customers, products, segments,
rules) for deeper recall; the always-inject identity block uses only the
'business_profile' entity.

## 2. Context Composer read path — `assemble_context_bundle(tenant_id)`
Reads the tenant's 'business_profile' entity via `search_entities(tenant_id,
entity_type='business_profile', limit=1)` (no query_embedding → created_at-DESC;
tenant_connection → RLS real), renders a compact token-bounded system block from
its attributes, returns None if absent/empty. Lives in `knowledge/l1.py`
(NOT a new module — avoids a second l1.py).

**D2 (read path) — DECIDED:** pre-inject the block as a SEPARATE system block
AFTER the VT-194 cached static prefix (never inside it; not a tool). Tenant
identity is always-relevant + cheap, unlike situational L0/graph recall (which
stay tool/`search_entities`-accessed).

## 3. Write path
**No L1 writer/seed exists in main yet** (only the read API). v1 (Phase 3,
Cowork-drafted, Fazal-reviewed): an idempotent seed INSERT of the RKeCom
'business_profile' entity into l1_entities + owner-dashboard edits. DEFER agent
L0→L1 auto-promotion to post-launch.

## 4. RLS + isolation (already in place)
mig 019 enables FORCE RLS + `tenant_id = app_current_tenant()` on l1_entities /
l1_relationships. Real-DB rls_tester denial test mandatory (VT-263 lesson: make it
a REAL RLS check — seed a B-owned entity + assert it is invisible under A's GUC,
not a WHERE-clause-shaped tautology).

## 5. get_business_profile reconcile
`get_business_profile.py` still SELECTs `owner_curated_context FROM
tenant_l1_profile` — an orphaned forward-target stub (that table was never built).
Reconcile it to read the 'business_profile' entity's
`attributes->>'owner_curated_context'` from l1_entities; keep graceful-None when
no entity exists.

## 6. L0 coexistence
L0 = workspace-wide cohort priors (tool-accessed). L1 = per-tenant entity graph
(identity pre-injected; deeper facts via search_entities). They do not overlap:
L0 = "what works for businesses like this"; L1 = "what is true about THIS
business." L0→L1 promotion deferred.

## Phasing (see `.viabe/queue/VT-195/plan.md`)
- **Phase 1** — assemble_context_bundle read + get_business_profile reconcile +
  real-DB rls_tester test + structural canary. No migration.
- **Phase 2** — pre-inject the block into the agent invocation (D2); canary =
  agent reasoning references a real l1_entities fact.
- **Phase 3** — Cowork-drafted RKeCom seed entity + writer + dashboard read.
- **VT-197** — day-39 → reflection write-back into L1 (follows Phase 1–2).
