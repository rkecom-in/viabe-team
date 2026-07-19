> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

---
title: Agent-framework target architecture — reconciliation of Fazal's definition vs VT-649/650 (as built)
author: claudecode
status: design-feedback (NOT a build authorization)
ts: 2026-07-16
re: [VT-101, VT-649, VT-650, VT-658, VT-659, VT-604, brain-central §7.0, effect-boundary]
grounding: direct repo inspection 2026-07-16 (agent_framework/*, agents/customer_send.py, agents/sales_recovery_executor.py, agent/integration_agent.py, agent/roster.py, agent/advisory_registry.py, .viabe/sprint/vt659-sr-framework-migration-plan.md)
---

# Reconciliation: Fazal's target (Manager / SubAgents / Tools) vs what VT-649/650 actually built

> This is design reconciliation for Cowork to finish into the canonical doc. Objective (Tier-1=0 aggregate) stays primary; this ran in parallel. **Two facts up front that change the framing** — verify these before anything else:
>
> 1. **The migration is UNBUILT.** VT-658 (Integration thin adapter) has **zero trace** in the repo — no sprint row, no code. VT-659 (SR migration) is **design-only** (`.viabe/sprint/vt659-sr-framework-migration-plan.md`, `status: design-complete`, cutover "DEFERRED Fazal-authorized"). The task board marking #108/#109/#110 "completed" is drift — the repo is the ground truth (Rule #14).
> 2. **What IS built (VT-649/650) is INERT.** The `agent_framework/` contract exists but is wired into **no live path**. `default_registry()` is empty; importing it changes zero routing. Live sends/money still flow through the pre-framework chokepoints (`customer_send.agent_send_draft`, `business_action_context`), which live paths call **directly** — never through the framework's `GateFacade`.
>
> So: the *target substrate* the migration would move onto exists; the migration itself does not. This is GOOD for reconciliation — nothing live has to be un-wired to adopt Fazal's model; we're choosing the shape before we build the cutover.

---

## 1. Current SR / Integration migration understanding (the thin-adapter-first plan as it stands)

**Integration TODAY (`agent/integration_agent.py`, VT-206/608):** a full brain-bearing **spawnable specialist** — `create_agent(model=resolve_chat_model("specialist"), tools=…, system_prompt=INTEGRATION_AGENT_SYSTEM_MESSAGE)`, a registered roster row (`spawn_integration`). It has its own LLM reasoning loop and 10 context-scoped `@tool`s (OAuth, pull_sample, propose_mapping, commit_ingestion, …). **But its money/ledger seam is already proposer-shaped:** it holds no write tool for the customer/ledger substrate; `commit_ingestion` returns a typed **proposal only** (VT-268 RULING 3), and the real write runs server-side deterministically (`integrations.commit.execute_pending_ingestion_commit`) from a **non-agent** code path. So structurally it is an autonomous agent, but on effects it already can't write directly.

**SR TODAY — already proposer/executor-split (pre-dates the framework):**
- **PROPOSER** (`agent/sales_recovery.py::run_sales_recovery_agent`): Tier-2 **pure** — no `tenant_connection`, no `.execute`, no send, no mutation. Consumes a pre-built `SalesRecoveryContext`; server owns the cohort (`_server_target_cohort`, mirrors VT-499/651). Returns an `AgentResult`.
- **EXECUTOR** (`agents/sales_recovery_executor.py::SalesRecoveryAgent.execute_item`): consent re-check → eligibility → detect_lapsed (45d) → draft+validate → persist → **ARM** (L3 hold or L2 approval). Terminal = `awaiting_approval` + `batch_id`. **It ARMS; it does not SEND.** The send is a separate downstream step (`approval_resume`/`l3_hold` → `agent_send_draft`).

**Roster reality (VT-604, built):** exactly **three** Phase-1 spawnable specialists — `sales_recovery_agent`, `integration_agent`, `onboarding_conductor`. Six advisory lanes (sales/marketing/finance/accounting/tech/cost_opt) were **demoted** from spawnable nodes to **Manager-held advisory tools** (`agent/advisory_registry.py::ADVISORY_TOOLS`, added to the Manager's own inventory) — read/analyse/prepare/draft only, no send/spend (VT-268 `assert_agent_tools_safe`).

**DB access reality:** decentralized. Both Integration tools and the SR executor open `orchestrator.db.tenant_connection` **directly**, keyed on the server-**resolved** tenant, with `conn=`-threaded RLS wrappers (`detect_lapsed_customers(…, conn=conn)`, `CustomersWrapper().send_eligibility(…, conn=conn)`, `enter_l3_hold(…, conn=conn)`). The conn-trust caveat is explicit in code (integration_agent.py:553 — "RLS-scoped write keyed on the RESOLVED tenant — never the raw BYPASSRLS pool keyed on a model-supplied tenant"; `resolve_lane_tenant` — ambient dispatch context always wins). **This is the opposite of Fazal's "only the Manager has DB access."**

**The thin-adapter-first plan (VT-659, design-only):** wrap SR's existing proposer/executor as a registered dual-role `agent_framework` module (manifest + `assert_conforms`), `AgentResult→ModuleResult` adapter, `CoordinatorAgentAdapter` for the executor role, activation-bar reuse — **EDIT ZERO existing SR files**, money-path cutover deferred. It formalizes the split SR already has; it does not change SR's behavior.

---

## 2. Point-by-point reconciliation (A–E) — fits / changes / risk

### A — Multi-brain vs brain-central (§7.0)

**Fit:** Fazal's "each sub-agent has its own brain" is a **reshape** of the current two-role contract, not a small extension. Today the `agent_framework` contract has **no central Manager brain object at all** — a module is split into a mechanical `propose()` (LLM lane, no effects) and `execute()` (arms via the gate), and the "decision" that matters (autonomous vs owner-approval) is **deterministic** (the gate), with the deciding brain assumed to be an out-of-frame caller. §7.0 "one central brain decides on every trigger" lives only in planning docs, not in this code. So there are **two different mental models in the tree right now** and Fazal's definition picks a third.

**Concrete mapping that satisfies both:** keep the deterministic gate as the effect authority (below), and let PROPOSER/EXECUTOR become the **internal mechanism of a gated tool**: the sub-agent brain reasons → emits a **tool call** (propose) → the deterministic gate executes after checks. The sub-agent brain sits **above** that tool. i.e.:
- **Manager brain** = the only always-on reasoner + the only sub-agent invoker (unchanged from today's supervisor role).
- **SubAgent brain** = a spawned reasoner (SR/onboarding already are). Its "propose" is a tool-call into a **gated tool**; its "execute" never touches transport directly.
- **Gate** = deterministic, unchanged, still the sole effect authority.

**Cost/latency/control delta of N brains vs one:** each spawned brain is a full LLM loop (Integration = Sonnet-tier, its own prompt). N brains = N× reasoning cost + serialized handoff latency (Manager → spawn → sub-agent loop → return). Today only 3 specialists spawn, and T9 **suppresses** two of them on answerable turns (`ANSWERABLE_SUPPRESSED_ROUTE_KEYS = {spawn, spawn_integration}`) precisely to avoid paying that cost when the Manager can answer in-turn. **Containment recommendation:** distributing reasoning does NOT distribute effect authority — Tier-1 containment stays intact **iff** every sub-agent brain's only route to an effect is a gated tool (B). Spawn a brain only when the work genuinely needs its own reasoning loop + tool surface; otherwise keep it a Manager-held advisory tool (the VT-604 pattern already does this for 6 lanes).

### B — Effect rails survive the "tools do effects" framing

**Fit — strong, but with a wiring gap to close.** The invariant holds cleanly in principle: the registry **structurally cannot** register a raw un-gated effect. Two independent guards:
1. **No raw-effect capability exists.** The `Capability` catalog's strongest declarable effect is `REQUEST_CUSTOMER_SEND` / `REQUEST_BUSINESS_ACTION` — both in `GATED_CAPABILITIES`, both serviced only by `GateFacade`. There is no capability that means "send directly."
2. **Deny-list on the tool objects.** `register()` runs `assert_agent_tools_safe(manifest.tools)` (VT-268) — a module holding a `send_whatsapp_*`/`write_ledger`/accounts tool is rejected. And `manifest.validate()` rejects a `{PROPOSER}`-only module that declares a gated capability (gated cap ⇒ EXECUTOR role required).

So "a sub-agent brain calling a send tool does NOT bypass the gate" is **enforced by construction** — a gated tool IS the GateFacade method, and there is no un-gated alternative to register.

**The gap (must fix at cutover):** `GateFacade` currently **only routes to** the real chokepoints (`agent_send_draft`, `business_impact_choke`) — no live path goes through it, and its `gate_business_action` returns only a *decision*, not the executed effect (the effect still must be issued inside `business_action_context`). So "GateFacade maps cleanly onto gated tools" is **true for customer-send** (it wraps the SOLE send path `agent_send_draft`, which runs all 7 gates: onboarded → WABA → batch/approval → template/opt-out-line → opt-out → consent → caps → transport-choke idempotency) but is **only half-wired for business/money actions** (it classifies, the caller still owns the `business_action_context` issue). **Risk:** if the doc says "tools do effects" without pinning that a money-tool must both classify (facade) AND issue inside `business_action_context`, a future sub-agent could get an AUTONOMOUS decision and then… have no facade path to actually perform it — or worse, perform it outside the choke. Recommendation: the gated-tool contract must own the *whole* round-trip (classify + issue-inside-choke), not just the decision.

### C — "Only Manager has DB access" + "sub-agents use Tools for DB"

**This is the biggest DELTA from today, and the riskiest.** Current reality (§1): SR-executor and Integration each hold their **own** `tenant_connection` and thread `conn=` into RLS wrappers. Fazal's target inverts that.

**Mechanics that make it work without breaking RLS:**
- **Manager owns the tenant-scoped RLS session.** The resolved tenant (IDOR-guarded, `resolve_lane_tenant` — ambient context wins over any model-supplied id) is established once at the Manager boundary. DB-touching **Tools** operate **within that scope** — they receive the resolved tenant + (optionally) the live `conn`, never mint their own from a raw pool.
- **Sub-agent brains never hold a raw connection.** They call a DB **Tool** (read_customer_ledger, read_integration_state, detect_lapsed, …). The tool is the only thing that sees a connection.
- **This sits ON TOP of the existing `conn=`-param design, but tightens its trust caveat.** Today the `conn=` param is a performance/lifetime optimization ("a cheap own RLS connection just for the read — NOT held across the seam"; "a hold must never pin a pooled connection"). The wrapper-conn-param **audit concern** (a wrapper trusting a caller-supplied `conn` whose RLS GUC / SET ROLE may not match the intended tenant) becomes **more** acute when a sub-agent brain is the caller. **Recommendation:** the DB-Tool boundary must (a) take the **resolved** tenant only (never a brain-supplied one), (b) open/scope its own RLS connection OR validate an injected conn's `app.tenant_id` GUC matches, and (c) never expose a raw connection object to the brain's tool-arg surface. This is a real migration cost: SR-executor + Integration tools currently assume they own the connection.

**Risk:** this is the change most likely to introduce an RLS/isolation regression (the VT-621 GUC-pool class). It should be the **last** thing migrated, behind the SR/onboarding proof, with the cross-tenant isolation rails (VT-603/DF1) re-verified.

### D — Integration dissolves into Tools

**Fit — directionally right, and it SHRINKS VT-101.** Fazal's "Integration is no longer an agent; 3rd-party actions are Tools" matches the existing "Integration = pure proposer, thin-adapter-first" scoping AND the VT-268 posture (Integration already can't write; commit is a server-side deterministic step). The re-scope is real:
- **From:** "migrate two agents (Integration + SR) onto the contract."
- **To:** "define a **Tool registry** (Shopify/GST/Sheets/email/file-upload as Tool definitions, each a decoupled action) + port SR onto {brain + tools}." Integration's brain **dissolves** — its connector logic (already mostly deterministic: Shopify fixed canonical mapping, Sheets wraps the VT-209 reasoner) becomes Tool definitions callable by SR **and any sub-agent**, not a spawnable specialist with its own LLM loop.
- **VT-101 delta:** the "Integration agent" line item collapses to "connector Tools in the registry." That removes one whole brain (cost + latency + one roster row + T9 suppression special-casing) and its 10-tool prompt surface. Net: VT-101 gets smaller and the effect surface gets simpler (fewer brains = fewer places to contain).

**Watch:** Integration today carries genuinely-conversational beats (OAuth back-and-forth, mapping confirmation, `integration_escalate_to_fazal`). "Dissolve to Tools" must not lose the **owner-facing conversational loop** — those beats move to the Manager (or a thin onboarding/connect sub-agent) driving the connector Tools, not vanish. Zero-manual-paste (CL-421) is a Tool-level property that must survive.

### E — Roster vs VT-604

**Agree with the split Fazal implies, with one framing correction.** VT-604 demoted 6 lanes to advisory **tools** because they had no activation bar / no durable task / no specialist-return — i.e., they were never real sub-agents. Fazal's list **re-promotes** Sales/Marketing/Compliance/Finance/Data/Online-presence to **brained sub-agents** for the *framework's* N-design. Those are two different time horizons:
- **Framework:** design for N brained sub-agents (the contract already supports arbitrary registered modules). ✅ agree.
- **Launch:** prove the pattern on **SR (+ onboarding)** only; the other six stay **Manager-held advisory tools** (their current VT-604 state) until a real tenant needs them as brained agents. ✅ agree — do NOT re-promote all six now; that's 6 new brains of cost/latency/containment surface for lanes with no launch customer. Promote a lane to a brained sub-agent only when it has (a) an activation bar, (b) durable task/plan participation, (c) its own effect Tools behind the gate — the exact bar VT-604 used to demote them.

**Flag:** the re-promotion should be **demand-driven per tenant**, not a big-bang roster change. The framework makes adding a brained sub-agent cheap (register a conforming module); use that to add them one at a time.

---

## 3. Proposed skeleton for the canonical "how it works" doc (Cowork to finish)

```
# Viabe Team — Agent Framework (Manager / SubAgent / Tool)

## 0. One-paragraph model
   Manager = the only always-on brain + the only sub-agent invoker + the only holder of the
   tenant-scoped DB session. SubAgents = spawned own-brain programs the Manager triggers on
   events. Tools = decoupled, gated actions (incl. all 3rd-party integrations). Effects happen
   ONLY through gated Tools; the deterministic gate is the sole effect authority.

## 1. The three roles — contracts
   1.1 Manager
       - owns: the RLS session (resolved tenant), sub-agent dispatch, in-turn answering,
         advisory-tool inventory (VT-604 shelf).
       - never: performs an effect except via a gated Tool.
   1.2 SubAgent  (protocol: propose(ctx, gate) / execute(ctx, gate))
       - has: own brain (LLM loop), own tool surface, an activation bar, durable task/plan
         participation, specialist-return.
       - never: holds a raw DB connection; never a transport handle; never an un-gated effect.
   1.3 Tool  (registry entry: manifest + capability + deny-list-checked tool objects)
       - kinds: READ tools (DB/context, scoped to the Manager's resolved tenant),
         GATED-EFFECT tools (customer-send, business/money action), INTEGRATION tools
         (Shopify/GST/Sheets/email/file — zero-manual-paste, CL-421).
       - invariant: no capability means "effect directly"; every effect Tool = a GateFacade
         method that runs the deterministic gate AND issues inside the transport/business choke.

## 2. The gated-tool boundary (the non-negotiable)
   2.1 customer-send  -> agent_send_draft (7 gates: onboarded, WABA, batch/approval, template+
       opt-out-line, opt-out, consent, caps) + customer_send_context idempotency.
   2.2 business/money -> assert_or_gate_business_action (policy + per-class autonomy tier) AND
       the effect issued inside business_action_context (else UngatedBusinessActionError).
   2.3 registration guards: manifest.validate (gated cap ⇒ EXECUTOR role) + assert_agent_tools_safe
       (deny raw send/spend/ledger/config-write tool objects). A raw un-gated effect Tool is
       UNREGISTRABLE by construction.

## 3. The DB-access rule
   - Manager establishes the resolved-tenant RLS scope once.
   - DB Tools operate within it: take the RESOLVED tenant only, never a brain-supplied id;
     own/scope their RLS connection or validate the injected conn's app.tenant_id GUC.
   - SubAgent brains never see a connection object.
   - Cross-tenant isolation rails (VT-603 / resolve_lane_tenant / DF1) re-verified at cutover.

## 4. Lifecycle
   trigger (owner msg / event) -> Manager reasons -> {answer in-turn | call advisory tool |
   spawn SubAgent} -> SubAgent brain proposes via gated Tool -> deterministic gate decides
   (autonomous | owner-approval) -> effect issued inside choke -> specialist-return to Manager.

## 5. Launch scope vs framework scope
   - Framework: design for N brained SubAgents.
   - Launch: prove on SR (+ onboarding). Integration DISSOLVES into connector Tools (no brain).
     The other 6 lanes stay Manager-held advisory tools; promote to brained SubAgent per-tenant,
     on demand, behind an activation bar.

## 6. Migration state (as of 2026-07-16)
   - BUILT: agent_framework contract (VT-649/650), INERT (no live wiring).
   - DESIGN-ONLY: SR migration (VT-659, cutover deferred). UNBUILT: Integration adapter (VT-658).
   - Live effects still flow through agent_send_draft / business_action_context directly.
```

---

## 4. The one thing I'd change in Fazal's definition

"Tools do the data exchange" + "only the Manager has DB access" are in mild tension for **reads**: a SubAgent that needs customer/ledger data will call a READ Tool every time, and that Tool needs the Manager's RLS scope. That's fine — but it means **Tools are not fully decoupled from the Manager's session**; a DB Tool is decoupled in *definition* but bound to the Manager's resolved-tenant scope at *invocation*. The doc should say this explicitly so nobody builds a "standalone" DB Tool that mints its own connection (the exact RLS-isolation footgun). Everything else in the definition holds and is directionally where the code already leans (SR split, VT-604 advisory shelf, VT-268 no-direct-write).
