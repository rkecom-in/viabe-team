# L1 Tenant Context Substrate — Design

> **Status: Cowork-drafted, pending Fazal D1/D3.** CC drafted the strawman
> (2026-05-31); Cowork reviewed + endorsed it and folded its rulings (review
> 2026-05-31T00:05Z). **D2 (read path) + D4 (RLS) are DECIDED.** Only **D1
> (attribute set)** and **D3 (write path)** await Fazal sign-off. On Fazal's
> greenlight, VT-195 Phase 1 builds (plan-ready per phase). No production
> code/migration ships before that. See `.viabe/queue/VT-195/plan.md` for phasing.

## 0. Purpose
L1 is the **per-tenant knowledge substrate** the orchestrator-agent reads so it
behaves as *this* business's agent, not a generic one (Fazal's moat directive,
2026-05-27). L0 (VT-126) = workspace-wide cohort priors; L1 = tenant-scoped.
L2–L4 = Mem0, deferred post-launch (CL-324). Hand-built Postgres + pgvector
(no Apache AGE — unsupported on Supabase).

## 1. Schema — `tenant_l1_profile` (aligns to the existing forward-target read)
`get_business_profile.py` already probes `SELECT owner_curated_context FROM
tenant_l1_profile`, so the table name is fixed. **v1 columns (D1 — Cowork: keep
L1 = durable per-tenant IDENTITY only; Fazal confirms the set):**

```sql
CREATE TABLE public.tenant_l1_profile (
    tenant_id              UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    -- Structured, predictable per-tenant attributes:
    business_archetype     TEXT,        -- e.g. 'electronics_retail' (RKeCom)
    owner_persona          TEXT,        -- short summary: tone, risk appetite, language
    integration_map        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {connector: role}
    working_hours          TEXT,        -- free text or structured later
    escalation_thresholds  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {cohort_size_max, spend_max_paise, ...}
    communication_prefs    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {default_language, formality, ...}
    -- Free-form owner/agent notes (the field get_business_profile already reads):
    owner_curated_context  TEXT,
    -- Audit:
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by             TEXT         -- 'cowork_seed' | 'owner_dashboard' (no 'agent_promotion' in v1)
);
ALTER TABLE public.tenant_l1_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_l1_profile FORCE ROW LEVEL SECURITY;
CREATE POLICY l1_select ON public.tenant_l1_profile FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY l1_insert ON public.tenant_l1_profile FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY l1_update ON public.tenant_l1_profile FOR UPDATE
    USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant());
```
**D1 ruling (Cowork, Fazal confirms):** DROPPED from v1 — `derived_stats` +
`derived_stats_at` (a stats cache → staleness risk; compute at read-time from
observability/attributions instead, §2) and `context_embedding` (no semantic
recall in seed-only v1 → defer to v2). Leaner = truer to "identity, not cache."

## 2. Derived stats — computed at READ time (no cached column, D1 ruling)
`assemble_context_bundle` computes any needed rolling stats (drop-off, response
rate, attribution summary) at read time from the live observability/attribution
tables, scoped to the tenant. Deterministic, no LLM (Pillar 1). No nightly batch,
no `derived_stats` column → no staleness. (If read-time cost becomes a problem
post-launch, revisit a materialized view — not a hand-maintained column.)

## 3. Archetype validation (D1 support)
Strawman validates against: (a) RKeCom electronics retail, (b) a tiffin/food
service, (c) a services freelancer. Each must map cleanly onto the columns
without overflowing into free-text-only. If they don't, the column set is wrong.
(Cowork to run this with Fazal during review.)

## 4. Context Composer read path — `assemble_context_bundle(tenant_id)`
A helper reads the tenant's L1 row (RLS-scoped) and renders a compact context
block (structured attrs + owner_curated_context + key derived stats), token-bounded.
**D2 (read path) — DECIDED (Cowork, technical):** PRE-INJECT the L1 block as a
SEPARATE system block AFTER the VT-194 cached static prefix — never inside it (a
per-tenant block inside the prefix would break the shared cache → cost blow-up).
Pre-inject, NOT a tool: tenant identity is always-relevant + cheap, unlike
situational L0 (which stays tool-accessed). The L1 block may carry its own
per-tenant `cache_control`, or be uncached if small.

## 5. Write path
**D3 — Cowork recommendation, Fazal confirms:** v1 = **seed-only + owner-dashboard edits**:
- Cowork drafts the initial RKeCom L1 row; Fazal reviews/edits (seed migration or
  admin insert; `updated_by='cowork_seed'`).
- Owner edits via dashboard (write surface = VT-198 owner-feedback work).
- **Defer** agent L0→L1 auto-promotion (auto-discovery is already "Out of scope"
  in the row). Promotion is the strongest moat lever but the riskiest (the agent
  writing its own durable per-tenant memory needs guardrails) — post-launch.

## 6. RLS + isolation (D4 — decided)
FORCE RLS, `tenant_id = app_current_tenant()` (mirror customers/mig 045). L1 reads
are tenant-scoped only; no cross-tenant sharing (CL-385/389/390). Real-DB
rls_tester denial test mandatory (tonight's VT-254 lesson: non-superuser role,
not mocks).

## 7. L0 coexistence
L0 = workspace-wide cohort priors (tool-accessed). L1 = per-tenant (pre-injected
or tool, per D2). They do not overlap: L0 answers "what works for businesses like
this," L1 answers "what is true about THIS business." Promotion (L0 pattern →
L1 tenant fact) is the bridge — deferred per D3.

## Canary (Rule #15, provisional)
Real API/DB assertions: (1) schema + RLS isolation under rls_tester; (2)
`assemble_context_bundle` returns the seeded row's fields scoped to the tenant and
NOTHING for another tenant; (3) the rendered block is token-bounded; (4) agent
invocation includes the L1 block and the response references an L1 field (proves
the moat actually reaches the model). Fail-not-skip.

## Decisions summary
- **D1 (attribute set)** — Cowork-recommended v1 column set (above); **Fazal confirms.** PENDING.
- **D2 (read path)** — DECIDED (Cowork): pre-inject separate block after the cached prefix.
- **D3 (write path)** — Cowork-recommended seed-only v1; **Fazal confirms.** PENDING.
- **D4 (RLS)** — DECIDED: FORCE RLS, `tenant_id=app_current_tenant()`, real-DB rls_tester test.

**Build gate:** VT-195 Phase 1 builds once Fazal signs off D1 + D3. D2/D4 settled.
