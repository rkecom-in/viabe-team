# Viabe Team — Agent Capability Framework (ACF) — Manager / SubAgent / Tool

**Status: CANONICAL — ratified by Fazal 2026-07-16. State sections updated 2026-07-18 (post-VT-101 build; §7).**
Author: Cowork, 2026-07-16, from Fazal's target definition (2026-07-16) + CC's code-grounded
reconciliation (`docs/archive/agent-framework-target-reconciliation.md`, archived). Grounded in the
VT-649/650 contract as built (`apps/team-orchestrator/src/orchestrator/agent_framework/`).

This is the canonical definition of how the Viabe agent system works. The builder's guide
(`docs/agent-framework/README.md`) and the SR tutorial defer to this doc on architecture.
`.viabe/manager-objective.md` remains the behavioral north-star; this doc is the structural one.

---

## 0. The model in one paragraph

**The Manager is the only always-on brain, the only component that invokes SubAgents, and the
only holder of the tenant-scoped DB session. SubAgents are modular, decoupled, own-brain
programs the Manager triggers on events or requests. Tools are decoupled, registered actions —
including every third-party integration — and all data exchange flows through them. Effects
(customer sends, money/business actions) happen ONLY through gated Tools; the deterministic
gate is the sole effect authority, no matter which brain is asking.** Intelligence is wide and
distributed; authority over effects is narrow and central. That split is the whole design.

## 1. The three roles

### 1.1 Manager (the embedded agent)
- **Owns:** the resolved-tenant RLS session (established once at the dispatch boundary,
  IDOR-guarded — ambient context always wins over any model-supplied id); sub-agent dispatch;
  in-turn answering; the advisory-tool inventory (the VT-604 shelf); planning/allocation/
  validation per the manager objective (§7 of `manager-objective.md`).
- **Is:** the only always-on reasoner and the HEAVIEST reasoner (Fazal 2026-07-16). Every
  trigger (owner message, scheduled event, callback) reaches the Manager's brain first. Its
  reasoning arc per request: understand the owner's requirement → identify the specialist
  capability → **define the outcome** → cross-validate the outcome with the owner ONLY when
  ambiguous or high-impact (never re-ask a known fact) → assemble the framing context →
  spawn and delegate → **validate the returned outcome against the outcome it defined**
  (approve/disapprove, logged with reasons — VT-514).
- **Delegation contract:** the Manager passes `situation` + `desired_outcome` +
  `context_slice` (the built `ModuleContext` payload). It gathers the FRAMING data — resolved
  tenant scope, the situation, the starter slice — NOT every operational datum the specialist
  might need (pre-fetching everything forces over-fetch or under-fetch; the specialist pulls
  operational data itself via Manager-scoped READ tools, §1.3/§3).
- **Model tier:** reasoning cost follows reasoning responsibility — heaviest model on the
  Manager; specialist tiers on SubAgents.
- **Never:** performs an effect except via a gated Tool; never delegates effect authority to
  its own reasoning (the gate decides, not the brain). Owner cross-validation of the OUTCOME
  is discretionary; the deterministic APPROVAL gate before an effect is not (§2) — the two
  must never be conflated.

### 1.2 SubAgent (spawned specialist)
- **Has:** its **own brain** (LLM reasoning loop), its own declared tool surface, an activation
  bar (entitlement/prereq-gated), durable task/plan participation, and a specialist-return to
  the Manager.
- **Is:** a program, triggered by the Manager on events/requests — never self-triggering in
  Phase 1.1 (dynamic sensing is Phase 1.2, held).
- **Contract:** a registered `agent_framework` module — manifest (positive capability
  allow-list) + role(s) + `assert_conforms`-clean. The PROPOSER/EXECUTOR split is the
  **internal mechanism of its gated tools**: the sub-agent brain reasons, its "propose" is a
  tool-call, and the deterministic gate executes after checks. The brain sits above the tools;
  the gate sits between the tools and the world.
- **Brain scope (Fazal 2026-07-16):** the SubAgent brain reasons ONLY within its delegated
  lane — evaluate the delegated action, identify the plan, choose tools and logic, drive to
  the outcome. It is a LOOP, not a one-shot plan: tool results surprise (empty cohort, failed
  OAuth, mismatched data) and the brain re-plans within its lane. When it cannot deliver the
  defined outcome, it returns an HONEST calibrated decline to the Manager — never a forced or
  fabricated outcome. It does not re-litigate the outcome definition, re-scope the tenant, or
  converse with the owner directly (owner conversation is the Manager's).
- **Never:** holds a raw DB connection; never a transport handle; never any un-gated effect
  path. Distributing reasoning does NOT distribute effect authority.
- **Examples:** Sales Recovery (the launch proof), Onboarding Conductor. Future: Marketing,
  Compliance, Finance, Data Import/Export, Online-presence — each promoted per §5's bar.

### 1.3 Tool (registered, decoupled action)
- **Kinds:**
  - **READ tools** — DB/context reads (customer ledger, business context, integration state),
    always scoped to the Manager's resolved tenant (§3). **BUILT (2026-07-17/18,
    `tools_common.py`):** EIGHT common READ tools wrap the canonical existing readers (never
    re-implemented) — `read_customer_ledger_summary` (counts only, no PII),
    `read_business_context`, `read_integration_state`, `read_active_plan` (VT-673),
    `read_agent_memory` (VT-674: L3 prior via `lookup_pattern` — quarantine + k-anon
    structural), plus the three VT-675 resolve-first promotions (`get_recent_campaigns`,
    `get_attribution_data`, `query_customer_ledger` — wrappers, because the raw agent/tools
    functions take a model-supplied `payload.tenant_id`, the IDOR class). PLUS ONE common
    ADVISORY tool: `escalate` (VT-672, `COMMON_ADVISORY_TOOLS`). Pattern every future common
    tool copies: `resolve_lane_tenant` FIRST (ambient wins), structured error dicts (never
    raises), own RLS conn (the §3 sanctioned pattern until DB-inversion), deny-list-checked
    at import.
  - **GATED-EFFECT tools** — customer-send, business/money action. These ARE the gate (§2).
  - **INTEGRATION tools** — Shopify, GST portal, Google Sheets, email, CSV/file, MCP/API
    connectors. Third-party actions are Tools, not agents. Zero-manual-paste after OAuth
    (CL-421) is a Tool-level property and survives any reshuffle.
- **Contract:** a registry entry — manifest + declared capability + deny-list-checked tool
  objects. Tools do the data exchange; brains command, tools act.
- **The common-tool surface (Fazal rulings, 2026-07-18) — CATEGORIZED, not just reads:**
  - **DATA/READ** — customer-ledger-summary, business-context, integration-state (built) +
    recent-campaigns, attribution, PII-gated ledger query, plan/roadmap read.
  - **CUSTOMER_SEND (GATED)** — request_customer_send → `agent_send_draft` (7 gates) + arm.
  - **BUSINESS_ACTION (GATED)** — perform_business_action (whole round-trip, §2).
  - **ESCALATE** — ONE common escalate tool (per-lane duplicates consolidate into it).
  - **TASK/PLAN/AUDIT/EVAL** — report_item_status, emit_tm_audit, self_evaluate,
    schedule_followup.
- **Owner communication is MANAGER-ONLY (Fazal, confirmed).** There is NO owner-message
  tool in the common surface and never will be: a sub-agent returns an outcome or an honest
  decline to the Manager; the Manager does all owner conversation (§1.2). Do not add a
  specialist owner-message tool.
- **ACF-tracked capability gaps — ALL 4 CLOSED (2026-07-18, same day they were registered):**
  (1) unified escalate tool → VT-672 (`escalate` on `COMMON_ADVISORY_TOOLS`); (2) plan/roadmap
  read tool → VT-673 (`read_active_plan`); (3) on-demand memory read tool → VT-674
  (`read_agent_memory`); (4) the richer reads promoted into the common set → VT-675
  (resolve-first wrappers). `KNOWN_CAPABILITY_GAPS` is EMPTY and the
  `check_capability_gaps.py` gate is GREEN; the registry + honesty test re-arm automatically
  on the next named hole (register future gaps there, each with a board row).
- **Canonical inventory (VT-669, 2026-07-18, `tool_catalog.py`):** the single source of
  truth for every tool surface — name → kind → capability → gated? → PII posture (CL-390) →
  tenant scope → holders — as a code registry (introspection-backed, drift-guarded). The doc
  [`TOOLS.md`](./TOOLS.md) is GENERATED from it (`render_catalog_markdown`), never hand-typed.
  The generated [`TOOLS.md`](./TOOLS.md) is the AUTHORITATIVE surface inventory — this doc
  deliberately carries no count (hardcoded counts drifted twice; regenerate TOOLS.md via
  `render_catalog_markdown()` whenever a surface lands). The catalog DOCUMENTS the gates; it
  never widens them. NOTE on holder labels: TOOLS.md's holders are CODE-LEVEL surface owners —
  `integration_specialist` is the LEGACY holder name for the dissolved Integration brain's
  surfaces (the brain is gone per this doc; its tool surfaces remain, held under the old label
  until the §7.3-era cleanup renames them). Conceptually they are connector Tools.
- **Sufficiency, not just safety (VT-669):** the framework enforced tool SAFETY (deny-list +
  positive-capability manifest) but never SUFFICIENCY. `AgentManifest.required_tools` declares
  the tools a specialist's job REQUIRES to reach; the 9th conformance check
  `required_tools_reachable` fails-loud at boot if a required tool is not in the catalog OR not
  reachable (own `tools` surface OR the Manager-scoped common READ set). SR records its required
  reads while keeping `tools=()` (Manager-scoped, arm != send); Onboarding via its own surface.
- **Invariant (by construction):** no capability exists that means "perform an effect
  directly." The strongest declarable capabilities are `REQUEST_CUSTOMER_SEND` /
  `REQUEST_BUSINESS_ACTION` — both gated, both serviced only by the GateFacade. Registration
  rejects raw send/spend/ledger/config-write tool objects (`assert_agent_tools_safe`, VT-268)
  and rejects a PROPOSER-only module declaring a gated capability. **An un-gated effect Tool
  is unregistrable.** A `required_tools` entry is NEVER a raw effect — the strongest is a gated
  `REQUEST_*` door.

## 2. The gated-tool boundary (non-negotiable)

Every effect, from any brain, routes through the deterministic gate:

- **Customer send** → `agent_send_draft` — the SOLE send path, running all 7 gates in order:
  onboarded → WABA live → batch/approval (Pillar-7 L2/L3) → template + opt-out line → opt-out
  → consent → caps — then transport-choke idempotency. The emission gate additionally binds
  stated money values (count/scope/₹) to the DB (CL-2026-07-16): a claim contradicting the DB
  is deterministically blocked/rewritten and flagged Tier-1.
- **Business/money action** → policy classification (per-class autonomy tier) AND the effect
  issued **inside** `business_action_context` (else `UngatedBusinessActionError`). **The gated
  tool owns the whole round-trip — classify AND issue-inside-choke.** A decision without a
  choke-issued effect, or an effect outside the choke, is a contract violation. (This closes
  the half-wired GateFacade gap found in reconciliation: facade decision + caller-issued
  effect is NOT an acceptable end-state.)
- **Correctness gates never bend for a green run:** GST verify, ownership, consent, onboarded,
  opt-out. Opt-out wins immediately and irreversibly within a turn.
- **Approval liveness (VT-668, 2026-07-17 — learned live):** an owner approval must NEVER
  resolve into silence. While an approval is armed, the consuming task PARKS (`waiting_owner`,
  stall-sweep-exempt) instead of burning its retry ladder against the approval TTL; resolution
  guarantees a live consumer (re-drive + honest ack) or an honest-expiry message to the owner;
  a dead-lettered consumer surfaces to the owner and closes its dangling approval. Silence
  after explicit owner authorization is a Tier-1-grade breach of this contract.
- **Correction = revision (VT-667):** an owner correction to a pending campaign REVISES it —
  supersede the stale draft, re-dispatch with the combined brief, re-arm. Never a re-mint,
  never a deflection, never a double-arm. What the owner approves IS what sends, in content
  as well as count.

## 3. The DB-access rule

- **Only the Manager holds DB access.** It establishes the resolved-tenant RLS scope once per
  dispatch. Sub-agent brains never see a connection object — not as a tool argument, not in
  context.
- **DB Tools operate within the Manager's scope:** they take the RESOLVED tenant only (never a
  brain-supplied id), and either open/scope their own RLS connection or validate an injected
  conn's `app.tenant_id` GUC matches the resolved tenant. They never mint a connection from a
  raw/BYPASSRLS pool.
- **Explicitly:** a DB Tool is decoupled in *definition* but bound to the Manager's
  resolved-tenant scope at *invocation*. A "standalone" DB Tool that creates its own session
  is the RLS-isolation footgun and is forbidden.
- **Migration note:** today's SR-executor and Integration tools open `tenant_connection`
  directly — the opposite of this rule. This is the riskiest delta (VT-621 GUC-pool class):
  it migrates LAST, behind the SR proof, with cross-tenant isolation rails (VT-603 /
  `resolve_lane_tenant` / DF1) re-verified at cutover.

## 4. Lifecycle of a unit of work

```
trigger (owner msg / scheduled event / callback)
  → Manager brain reasons on FULL context (never re-asks a known fact)
      → answer in-turn                                (most turns; T9 suppression)
      → call a Manager-held advisory tool             (analysis/draft, no effect)
      → spawn a SubAgent                              (work needing its own loop)
          Manager first: defines the OUTCOME → cross-validates with owner ONLY if
          ambiguous/high-impact → assembles framing context (situation, desired_outcome,
          context_slice, resolved tenant scope) → spawns
          → SubAgent brain loops within its lane: plan → call Tools → re-plan on surprises
              → READ tools (Manager-scoped; operational data pulled as needed)
              → gated-effect tool: propose → deterministic gate decides
                    (autonomous | owner-approval L2/L3)   [NEVER discretionary]
                → effect issued inside the choke → audited
              → cannot deliver? → honest calibrated decline (never forced/fabricated)
          → specialist-return to Manager
  → Manager VALIDATES the returned outcome against the outcome it defined
    (approve/disapprove, logs decision + reason — VT-514), reports/asks owner
```

Owner-language: replies render in the owner's language per the per-tenant language preference
(elevated build, 2026-07-16 live-drive finding); deterministic template copy and brain replies
render against the same preference.

## 5. Launch scope vs framework scope

- **Framework:** designed for N brained SubAgents. Adding one = registering a conforming
  module. No architectural change per agent.
- **Launch:** prove the pattern on **SR + Onboarding Conductor only**. Integration **dissolves
  into connector Tools** (its brain is removed; its owner-facing conversational beats — OAuth
  back-and-forth, mapping confirmation, escalation — move to the Manager driving the connector
  Tools, and must not be lost). The six advisory lanes (sales/marketing/finance/accounting/
  tech/cost-opt) stay Manager-held advisory tools.
- **Promotion bar (per lane, per demand — never big-bang):** a lane becomes a brained SubAgent
  only when it has (a) an activation bar (entitlement ₹5000/agent + prereqs), (b) durable
  task/plan participation, (c) its own effect Tools behind the gate. This is the same bar
  VT-604 used to demote them.

## 6. Cost & containment posture

- Every SubAgent brain is a full LLM loop: N brains = N× reasoning cost + handoff latency.
  Spawn only when the work needs its own reasoning loop + tool surface (T9 already suppresses
  spawns on answerable turns). Default to Manager-in-turn or advisory tools.
- Tier-1 containment does not depend on any brain's honesty: fabricated money claims are
  deterministically bound to the DB; effects are deterministically gated; the DB asserts are
  the sole money Tier-1 authority (CL-2026-07-16). More brains widen the reasoning surface,
  not the effect surface.

## 7. Migration state (ground truth as of 2026-07-18)

- **VT-101 MIGRATION COMPLETE on dev** (CL-2026-07-16-arch-ratified-migration). All stages
  landed, each delta-gated on deployed dev and HELD:
  - Stages 0–2 (additive): SR as a dual-role module (VT-659, thin adapter, zero SR-file
    edits) · Integration surface as an 11-tool connector-Tools module (VT-664) ·
    `GateFacade.perform_business_action` owning the whole round-trip (§2).
  - Stage 3(a)+(b): **SR routes through the framework contract** — coordinator canary armed
    a real win-back with zero sends/zero errors; j01 clean.
  - Stage 3(c): **Integration's brain DISSOLVED** into Manager-driven connector Tools;
    owner-facing connect flow intact; VT-658 closed.
- **Flags:** `TEAM_SR_VIA_FRAMEWORK` + `TEAM_INTEGRATION_VIA_FRAMEWORK` — **ON in dev,
  OFF in prod.** Prod promotion rides the VT-231 cutover checklist (Fazal 2026-07-17,
  Pillar-7).
- **Common READ-tools layer BUILT** (§1.3, `tools_common.py` + `CommonToolsModule`).
  **Tool catalog / required-tools manifest / sufficiency conformance = VT-669 BUILT**
  (`tool_catalog.py` — the generated `TOOLS.md` is the authoritative, always-current inventory; `AgentManifest.
  required_tools` + the 9th `required_tools_reachable` conformance check; SR + Onboarding
  carry required-tools manifests). Additive/inert — no live routing change.
- **Approval seam hardened (2026-07-18):** VT-668 CLOSED (approval liveness, §2 — an owner
  approval can never resolve into silence). VT-667 core CLOSED (correction=revision +
  creative-brief threading proven on dev; latency/persona tail = VT-671).
- **REMAINING:** §7.3 DB-access inversion (§3) — Fazal-explicit LAST, not yet granted.
  Codex/third-party builders stay HELD until the post-migration full-pack re-aggregate
  re-proves Tier-1=0.

## 8. Ratification (Fazal)

Settled by Fazal 2026-07-16: the three-role model (Manager/SubAgent/Tool), sub-agents have
own brains, only-Manager-DB, Integration dissolves into Tools, and the reasoning split —
Manager = heaviest reasoner (understand → identify specialist → define outcome →
cross-validate with owner only if required → frame context → spawn → validate the return);
SubAgent brain = evaluate the delegated action, plan, tools, logic, outcome, within its lane.

Refinements folded in (CC + Cowork, need Fazal's nod): the gated tool owns the whole effect
round-trip (§2); DB Tools bound to Manager scope at invocation (§3); launch proves SR +
Onboarding only, six lanes stay advisory behind the promotion bar (§5); Manager gathers
FRAMING data, SubAgents pull operational data via Manager-scoped READ tools (§1.1); the
SubAgent brain is an iterative lane-bounded loop with honest calibrated decline (§1.2);
outcome cross-validation with the owner is discretionary, the effect approval gate never is
(§1.1/§2).

- [x] **RATIFIED — Fazal, 2026-07-16** (including the reasoning split + all three
  refinements). VT-101 re-scoped build granted same day ("close this tonight").
