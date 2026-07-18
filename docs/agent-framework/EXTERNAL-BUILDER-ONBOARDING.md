# External-builder onboarding — building specialised agents on the Viabe Team framework

**Audience: any EXTERNAL implementer with zero prior context on this repository** — an AI coding
agent (Codex, Claude, etc.) or a human contractor. This document is self-contained.
Sections 2, 3, 5 and 6 (the contract, tool rules, acceptance gates, workflow) are
ENGAGEMENT-AGNOSTIC — they apply to every module ever built on this framework, whoever builds
it. Sections 1 and 4 describe the CURRENT engagement (the Financial-Compliance module) and get
replaced per assignment. Read it top to bottom before writing code. It tells you what
you are building, the exact contract you must satisfy, the rules that keep your code safe to
merge, what data actually exists for you to read, the gates your PR must pass, and the mechanics
of how work moves through this repo.

Everything referenced here lives under `apps/team-orchestrator/src/orchestrator/` unless stated
otherwise. When this doc says "read `X`", it means read the real file — this doc summarizes it
accurately but the code is the ground truth if the two ever disagree.

Companion docs, in case you need more depth than this file gives you:
- `docs/agent-framework/README.md` — the 5-step build+verify walkthrough (Sales-Recovery example).
- `docs/agent-framework/ARCHITECTURE.md` — the canonical Manager/SubAgent/Tool structural model.
- `docs/agent-framework/TOOLS.md` — the generated inventory of every existing tool surface.
- `apps/team-orchestrator/src/orchestrator/agent_framework/modules/sales_recovery_module.py` — the
  reference example of a real, both-roles module built on this contract.
- `apps/team-orchestrator/src/orchestrator/agent_framework/modules/compliance_tools_module.py` —
  the skeleton this onboarding kit ships alongside this doc (VT-685). It is a WORKING, conforming,
  registerable module with one real read-only tool and `TODO(Codex)` markers at every extension
  point. Copy its shape; do not start from a blank file.

---

## 1. What you are building *(current engagement — replaced per assignment)*

You are building **independent specialised agents as FRAMEWORK MODULES** — small, self-contained
classes that plug into `orchestrator.agent_framework`, the platform's modular agent-integration
contract. A module **PROPOSES and/or EXECUTES**; the platform **ENFORCES every trust gate**. You
never reach for a database connection, a WhatsApp send, or a money action directly — you ask the
platform to do it through a narrow, capability-scoped door, and the platform decides whether that's
allowed.

**Your first target is `compliance_tools`** — a specialist for GSTR-1/3B **return-filing
READINESS** (does the tenant have what a filing needs: a verified GSTIN, sales history). **Later**
targets (not in scope yet, do not build them without a separate, explicit go-ahead): ROC/AOC
filings, balance-sheet readiness, broader audit readiness.

### Phase-1 posture: ADVISORY / PREPARE-ONLY — read this twice

Everything you build in Phase 1 **reads, analyses, and prepares**. It **never files, sends,
spends, or mutates business state.** Concretely:

- Your module returns checklists, readiness snapshots, prepared summaries — never a "filed"
  confirmation, never a receipt, never a claim that something external happened.
- Your module declares **zero gated capabilities** (defined precisely in §2) and is a pure
  `PROPOSER`. There is no "compliance EXECUTOR" role in Phase 1.
- Actual GST return filing is a **later graduation** — a real integration with the GST portal,
  gated behind its own capability-registry entry, its own verifier, and an explicit Fazal
  authorization. That entry already exists TODAY as a **declared-disabled honesty entry**
  (`compliance.return_filing` in `orchestrator/capability/registry.py`) so the Manager can tell an
  owner "I can't file that yet, but I can prepare your readiness checklist" — truthfully — from day
  one. **Do not build a filing/submit tool.** If you think Phase 1 needs one, stop and raise it as
  a question rather than writing it.

This mirrors an existing, shipped precedent you should read for calibration:
`orchestrator/agent/accounting_lane.py` — a lane that PREPARES tax summaries and categorized books
but has **no file/submit/transact tool on its surface, by design, enforced by a deny-list at
build time** (see §3). Your compliance module is the same shape, generalized onto the newer
`agent_framework` contract instead of the older lane shape accounting uses.

---

## 2. The contract, precisely

The whole contract lives in one Python package: `orchestrator.agent_framework`. Import
**everything** you need from that top-level package — never a submodule-deep path
(`orchestrator.agent_framework.manifest`, etc.). The package's `__all__` **is** the contract: if
your module compiles against `agent_framework` and `assert_conforms` passes, it is
integration-ready. Read `agent_framework/README.md` in full — it is short and it is the build
guide. What follows is the same contract restated for a from-scratch reader, plus the parts most
relevant to a read-only Phase-1 module.

### 2.1 `AgentManifest` — the declarative contract for your module

Defined in `agent_framework/manifest.py`. A frozen dataclass:

```python
AgentManifest(
    name: str,                                    # your module's stable registry key
    version: str,                                  # semver-ish; bump on a breaking manifest change
    roles: frozenset[AgentRole],                   # {PROPOSER}, {EXECUTOR}, or both
    description: str,
    capabilities: frozenset[Capability] = frozenset(),
    prerequisites: AgentPrerequisites | None = None,
    tools: tuple[Any, ...] = (),
    required_tools: tuple[str, ...] = (),
    entitlement_key: str | None = None,
)
```

Call `manifest.validate()` (or let registration/conformance call it for you) — it enforces, among
other things, the **positive-capability rule**: a gated capability (§2.3) is legal **only** when
`EXECUTOR` is a declared role. Compliance in Phase 1 declares `roles=frozenset({AgentRole.PROPOSER})`
— a pure proposer — so it can declare **no** gated capability at all, and `validate()` will reject
you if you try.

### 2.2 Roles — `PROPOSER` / `EXECUTOR`

Defined in `agent_framework/capabilities.py`. Every module declares a **set** of roles:

- **`PROPOSER`** — a conversational-lane module. Reads context, returns a **proposal** (an intent /
  draft / recommendation). **No side effects.** Its `GateFacade` (§2.4) is empty of gated
  capabilities — it *structurally cannot* send, spend, or file anything.
- **`EXECUTOR`** — a coordinator-dispatched module that does work against a claimed work item and
  arms consequential actions **only** through the `GateFacade`.

**Compliance starts `PROPOSER`-only.** Do not add `EXECUTOR` in Phase 1 — there is nothing for an
executor to do until real filing graduates (§4/§6), and adding it prematurely is exactly the kind
of scope-widening this doc tells you to avoid.

The role-to-method binding (`ROLE_METHOD` in `capabilities.py`) is fixed: `PROPOSER` → your class
must expose a callable `propose(self, ctx, gate)`; `EXECUTOR` → `execute(self, ctx, gate)`.
Registration checks this and rejects a module missing the method its declared role requires.

### 2.3 Capabilities — the POSITIVE half of the trust boundary

Defined in `agent_framework/capabilities.py`. Two families:

- **NON-GATED** (`READ_*`, `PROPOSE_*`) — reads and proposals; no side effect. A pure `PROPOSER`
  lives entirely here. For compliance's GSTR-readiness read you declare
  `{Capability.READ_BUSINESS_CONTEXT, Capability.READ_CUSTOMER_LEDGER}` — the two reads the
  snapshot actually performs (see the skeleton's manifest).
- **GATED** (`REQUEST_*`, listed in `GATED_CAPABILITIES`) — a *request* to run a consequential
  action. **Legal only if `EXECUTOR` is a declared role**, and serviced **only** by a `GateFacade`
  method that routes to a real deterministic gate. There is **no capability that means "file/send
  directly."** You will not declare any of these in Phase 1.

The invariant that makes this safe by construction: **no `Capability` value means "perform an
effect directly."** The strongest thing a manifest can declare is "ask the platform, through the
facade, to run a gated action" — and the platform still decides autonomous-vs-approval. You cannot
manifest your way to a raw transport.

### 2.4 `GateFacade` — why a module physically cannot reach an effect

Defined in `agent_framework/gate_facade.py`. This is **the trust boundary**. A module is handed a
`GateFacade` instance (never a raw gate module) built by the framework from your manifest's
declared capabilities, **scoped to the context's role**:

- `request_customer_send(...)` → routes to `customer_send.agent_send_draft` (the full fail-closed
  gate stack — onboarded check, WABA-live check, batch/approval, consent, budget caps).
- `gate_business_action(...)` / `perform_business_action(...)` → route to
  `business_impact_choke.assert_or_gate_business_action` (a SPEND/COMMITMENT/CONFIG decision, then
  — for `perform_business_action` — the effect issued **inside** the deterministic choke).

Call any gated method **without** having declared its capability, and it raises
`CapabilityNotDeclared`. For a pure `PROPOSER` (compliance, Phase 1), the facade you receive is
**empty of gated capabilities regardless of what you pass in `gate`** — even if you tried to call
`gate.request_customer_send(...)` from inside `propose()`, it would raise. This is proven for you
automatically: the conformance check `proposer_gate_readonly` (§5) calls every gated facade method
on a proposer-scoped facade and asserts each one raises. You get this guarantee for free by
declaring `roles={PROPOSER}` and no gated capability — you do not need to write any code to enforce
it.

**In Phase 1, your `propose()` method's `gate` argument is intentionally unused.** That is correct,
not a bug — every existing Phase-1-shaped module (`sales_recovery_module.py`'s proposer lane,
`integration_tools_module.py`, `onboarding_conductor_module.py`, the skeleton this doc ships with)
does the same and says so in a comment. Do not manufacture a use for `gate` just to "use" the
parameter.

### 2.5 `ModuleContext` — what a module receives (and the IDOR rule you must never violate)

Defined in `agent_framework/context.py`. Built via `ModuleContext.for_proposer(...)` (never the raw
constructor). The fields relevant to you:

- `tenant_id: UUID` — **the authoritative tenant.** This is resolved for you, not something you
  compute.
- `role: AgentRole`
- `data: Mapping[str, Any]` — a per-lane structured bundle, if the caller pre-built one.

**The hard rule (read this as many times as it takes to stick): NEVER accept a tenant identifier
from an LLM payload and trust it as the scope for a database read.** `ModuleContext.for_proposer`
resolves the tenant through `orchestrator.agent.lane_tenant.resolve_lane_tenant` — the **ambient
dispatch context wins**; a model-supplied tenant value that disagrees is logged and **ignored**,
never trusted. This closes a real, previously-shipped defect class (VT-293/294/599): a model once
filled a `tenant_id` parameter with a business **name** instead of a UUID, and — worse — a
model-suppliable tenant scope is a textbook IDOR (a compromised or confused model could reach
another tenant's data by supplying their id). **This is the "resolve-first IDOR rule" — cite it
back if you're ever tempted to add a tool parameter like `tenant_id: str` that gets used directly
as a DB scope.** The correct pattern, which `resolve_lane_tenant` implements and which
`compliance_tools_module.gstr_filing_readiness_snapshot` demonstrates:

1. A tool takes `tenant_id: str` as a parameter (yes — the signature keeps it, so existing
   prompts/bindings don't need to change).
2. The **first line** of the tool's body calls `resolve_lane_tenant(tenant_id, tool_name=...)`.
3. If it returns `None` (unresolvable), return a structured error dict —
   **never raise** (a raise here would orphan the caller's tool-use turn / hang the run).
4. Only the **resolved** value is ever passed to a DB read.

Inside your module's own `propose(self, ctx, gate)` method, you already have `ctx.tenant_id` —
the ALREADY-resolved, authoritative tenant. Use it directly; do not re-run `resolve_lane_tenant`
inside your own module methods (that function is for a standalone `@tool` a model calls directly,
where the tenant argument is genuinely untrusted model input). This is exactly what
`integration_tools_module.py`'s `_read_state` and the compliance skeleton's `propose()` do.

### 2.6 `entitlement_key` — the billing seam (do not build a price check)

Defined in `agent_framework/entitlement.py`. `entitlement_key` on your manifest is a
**self-describing SKU declaration** ("this agent is billable") — it is **not** a price and it is
**not enforced** by you. `check_entitlement(manifest, tenant_id)` computes IN-TRIAL-OR-ACTIVE-PAID
from the billing substrate, but it is **soft-open (`True`) until billing wires** — pre-launch, it
never hard-blocks. The compliance skeleton declares `entitlement_key="compliance_agent"` (the
₹5000/agent seam every specialist SKU uses). **You do not need to write any entitlement-checking
code** — this happens outside your module, later, when it's wired into the live activation path.
Do not hardcode a price anywhere.

---

## 3. Tool rules

"Tools" here means both (a) real, langchain-`@tool`-decorated Python functions you may put on
`manifest.tools`, and (b) the general shape of what your module is allowed to *do*. Read
`orchestrator/agent/tool_guardrail.py` in full — it is short and it is the enforcement mechanism.

### 3.1 Read / analyse / prepare only — enforced, not just documented

`assert_agent_tools_safe(tools, surface=...)` runs at **registration time** (and again by the
conformance suite, §5) over every tool your manifest carries. It checks each tool's `.name` against
a **deny-list** of forbidden capability substrings —
`send_whatsapp_message`, `send_to_customer`, `write_sheet`, `write_ledger`, `execute_spend`,
`make_payment`, `sign_contract`, `write_config`, and about a dozen more (the full list is
`FORBIDDEN_CAPABILITY_SUBSTRINGS` in that file). **If a tool you write matches any of these
substrings, registration raises `ModuleRegistrationError` and your module cannot be registered.**
This is the fail-closed backstop underneath the positive-capability model in §2.3 — you are held to
the exact same boundary as every hand-wired agent surface in this codebase, including the Manager
itself. Do not try to rename around it; a matching substring is a signal you are building the wrong
thing for Phase 1.

### 3.2 Every tool is ambient-tenant-resolved

Every tool you write follows the resolve-first pattern in §2.5. Copy the shape from
`orchestrator/agent_framework/tools_common.py` (the canonical example — read
`read_customer_ledger_summary` there top to bottom) or from
`compliance_tools_module.gstr_filing_readiness_snapshot` (the skeleton's own worked example, which
follows the identical shape). The three invariants every tool there documents, which yours must
also hold:

1. **Resolve-first, model-untrusted** (§2.5).
2. **Own RLS scope, never a passed connection** — a DB-touching tool opens its **own**
   `tenant_connection(resolved_tenant)` (or delegates to a reader that does); it never accepts a
   `conn` argument from a caller and never touches a raw/BYPASSRLS pool.
3. **CL-390 PII-safe** — a tool's return carries counts / IDs / statuses / the owner's own business
   fields only. **Never** a customer name, phone, or email. (A GSTR-readiness snapshot has no
   reason to ever touch customer PII in the first place — it reads aggregate ledger shape and the
   tenant's own GST verification status.)

### 3.3 No raw DB access outside the wrapper layer

Run `python scripts/check_no_direct_tenant_db_access.py` (from the repo root) before you consider
any DB-touching PR done — it is a **blocking** pre-push/CI gate. It forbids direct SQL against a
specific, small list of tenant-scoped hot tables (`customers`, `campaigns`, `pending_approvals`,
`owner_inputs`, `phone_token_resolutions`, `platform_listings`, `refund_executions`) **outside**
`orchestrator/db/wrappers/`. New code touching those tables must go through
`orchestrator.db.wrappers` (e.g. `CustomersWrapper`), never raw `conn.execute("... FROM customers
...")`.

**Important nuance for your compliance work:** `customer_ledger_entries` (the sales ledger table)
is **not** on that gated list. Direct SQL against it through `tenant_connection(...)` is the
existing, sanctioned pattern — see `orchestrator/agent/accounting_lane.py`'s
`_read_ledger_summary` and `compliance_tools_module._compute_gstr_readiness`, which does the same
thing. Don't invent a wrapper class for it; follow the accounting-lane precedent.

### 3.4 LLM calls — only through the resolver, never a raw client

If your module ever needs to call an LLM directly (Phase 1's read-only snapshot does not, but a
future richer compliance reasoning step might), you call
`orchestrator.llm.provider.resolve_chat_model(tier, *, agent, tenant_id=None, ..., call_site=None)`
— **never** construct a raw `ChatAnthropic` / Anthropic SDK client yourself. This function:

- Resolves the model id for `tier` (e.g. `"specialist"`) via the multi-provider seam so a model
  swap is an environment variable, not a code change.
- Wires the per-call **budget hook** and the **usage-recording ledger callback** (so your calls
  show up in per-tenant × per-agent cost metering) — bypassing it means your module's cost is
  invisible to the system.
- Takes `agent=` (your module's name — this is the metering attribution key) and `call_site=`
  (defaults to the tier name).

System prompts, if you ever add one, should be defined as a `SystemMessage` with a
`cache_control: {"type": "ephemeral"}` content block — copy the exact pattern at
`orchestrator/agent/accounting_lane.py:95` (`ACCOUNTING_LANE_SYSTEM_MESSAGE`), which every existing
specialist lane uses to amortize the system-prompt + tool-inventory cost across dispatches (VT-194
prompt caching).

---

## 4. Data available today for GSTR readiness *(current engagement — replaced per assignment)*

This is what actually exists for you to read. Do not invent fields; if the data isn't here, your
readiness check should honestly report its absence, not guess.

- **Sales ledger** — `customer_ledger_entries` (populated via the sheets/Shopify ingest paths under
  `orchestrator/integrations/`). Each row has `entry_type` (`'sale'` / `'payment'`), `entry_date`,
  `amount_paise`, `tenant_id`, `customer_id`. For readiness, you care about the SHAPE of sales
  history (which months have data, how recent the last sale is) — not the money value. Read
  `orchestrator/agent/accounting_lane.py`'s `_read_ledger_summary` for the established
  aggregation pattern (`GROUP BY entry_type`, counts + totals + date span), and
  `compliance_tools_module._compute_gstr_readiness` for the readiness-specific shape (distinct
  months present + trailing-90-day count).
- **`tenants.verification_status`** (the GSTIN verification tier) — read it through
  `orchestrator.knowledge.business_context.read_business_context(tenant_id)`, **not** a direct
  `SELECT ... FROM tenants` (that function is the manager's own canonical identity read; it already
  applies the correct "which tiers count as verified" boundary via
  `orchestrator.agents.onboarding_gate._VERIFIED_TIERS` — **never re-derive the verified/unverified
  boundary yourself**, always go through this read). The returned `BusinessContext.identity` dict
  carries `gst_status`, `gst_verified` (bool), `gstin_present` (bool), `business_name`,
  `verified_business_name`.
- **Customers + spend wrappers** — `orchestrator.db.wrappers.CustomersWrapper` (counts: `count_all`,
  `count_with_sales`, `count_lapsed`). Not directly needed for a GSTR-readiness check, but available
  if a later compliance feature needs customer-count context (e.g. "is this business even active
  enough to have a filing obligation" is a judgment call for Fazal, not something to encode as a
  hard gate yourself).

### MCA / ROC — hard boundary, do not cross without an explicit un-park

**There is no MCA (Ministry of Corporate Affairs) / ROC integration in this codebase, and building
one is explicitly out of scope until Fazal un-parks it (a standing decision).** ROC/AOC/
balance-sheet readiness work — if and when it is authorized — uses **OWNER-PROVIDED documents
only**: the owner uploads or pastes figures through the Manager (owner-communication is a
Manager-only capability per `ARCHITECTURE.md` §1.2 — a specialist module never talks to the owner
directly), never an automated scrape of the MCA portal or a third-party ROC data source. If a task
ever asks you to "pull the company's ROC filing status automatically," that is out of scope — stop
and flag it rather than building a scraper.

---

## 5. Acceptance checklist — what your PR must pass

Run these from `apps/team-orchestrator/` unless noted:

1. **Conformance harness green for your module.** Every module is verified by ONE call:
   ```python
   from orchestrator.agent_framework import assert_conforms
   assert_conforms(YourModule())
   ```
   in a test (fails the test at the first violation) — see
   `tests/orchestrator/agent_framework/test_compliance_tools_module.py::test_module_conforms` for
   the exact pattern. This runs the full 9-check suite (`has_manifest`, `manifest_valid`,
   `capabilities_legal_for_roles`, `tool_surface_safe`, `role_methods_present`,
   `proposer_gate_readonly`, `gated_capabilities_serviced`, `name_registerable`,
   `required_tools_reachable`) — see `agent_framework/conformance.py` for what each one asserts.
2. **`ruff check`** over every file you touched:
   ```
   uv run ruff check src/orchestrator/agent_framework/modules/<your_module>.py \
       src/orchestrator/capability/registry.py tests/orchestrator/agent_framework/test_<your_module>.py
   ```
3. **Dep-less smoke** — your module must import cleanly with **no** langchain/dbos/anthropic
   installed:
   ```
   uv run --no-project --isolated --with pytest --with pyyaml pytest -q
   ```
   This is the "importorskip discipline": any test file that imports something requiring
   langchain/dbos **must** start with `pytest.importorskip("langchain")` (or `"dbos"`) **before**
   the import, so it SKIPS at collection instead of erroring when the heavy dep is absent. Your
   production module file itself should stay import-light too — build any real `@tool` object
   **lazily** (inside a function called from `__init__`, never at module top-level) exactly like
   `compliance_tools_module._compliance_tools()` / `integration_tools_module._connector_tools()` do.
   You can sanity-check this directly:
   ```
   uv run --no-project --isolated python -c "
   import sys; sys.path.insert(0, 'src')
   import orchestrator.agent_framework.modules.<your_module>
   print('import OK — no heavy deps required')
   "
   ```
4. **Unit tests for every tool.** Every real `@tool` function you add needs a direct unit test of
   its resolve-first behavior (unresolvable tenant → structured error, never a raise) plus its
   actual logic (with the DB/knowledge reads injected/mocked — never a live DB in a unit test).
5. **Scope discipline — touch only:**
   - `agent_framework/modules/<your_module>.py` (your module),
   - its test file(s) under `tests/orchestrator/agent_framework/`,
   - capability registry entries in `orchestrator/capability/registry.py` (+ its test file) for any
     NEW capability your module's job needs declared,
   - this doc, if you're extending the onboarding kit itself.
   Do **not** touch the Manager's live dispatch wiring, the roster, the coordinator registry, or
   any other specialist's file. Registering your module (`register_agent(...)`) does **not** wire
   it into any live seam — that is a deliberate, separate, Fazal-reviewed step, not part of your PR.
6. **PR into `dev`, never `main`.** Claude Code (CC, the repo's primary implementer) reviews your
   PR. Merge authorization follows Pillar-7 (every merge requires an explicit Fazal-authorized
   signal) — you do not self-merge.
7. **Owner-facing copy triggers the ×3 measurement gate.** If any text your module produces is
   ever rendered to an owner (it shouldn't be, directly — the Manager renders owner-facing copy,
   not a specialist module — but if you add any user-facing string), it must be validated across
   ≥3 runs for consistency before being considered done, per this repo's standing measurement
   discipline. For Phase 1's structured readiness snapshot (a dict the Manager consumes, not raw
   owner-facing text) this is not yet in play — flag it if that changes.

---

## 6. Workflow mechanics

- **Branch naming:** `codex/<topic>` (e.g. `codex/gstr-readiness-notes`, `codex/compliance-roc-scaffold`).
- **Small commits.** One coherent change per commit; do not squash your whole PR into one giant
  commit — CC's review is easier against a readable history.
- **VT-row IDs are allocated, never invented.** This repo tracks work as numbered `VT-<N>` rows
  under `.viabe/sprint/`. **You do not have write access to the allocator** — ask CC (Claude Code,
  via whatever channel routed you this task) to allocate a VT-ID for your PR before you open it.
  Never hand-pick a number by scanning existing files (`scripts/vt_id_allocate.py` is
  flock-serialized specifically because directory-scanning collides under concurrent work — this
  bit the team twice before the allocator existed).
- **Migration numbers, same rule, if you ever add a DB migration** (Phase 1 compliance work should
  not need one — the ledger and tenant tables already exist). If a later phase needs a new table,
  ask CC to allocate the number via `scripts/migration_id_allocate.py` — never hand-pick by scanning
  `migrations/`.
- **PR review + merge:** open your PR against `dev`. CC reviews it against this doc + the
  acceptance checklist in §5. Merge requires Pillar-7 sign-off (Fazal-authorized) — this is not a
  step you or CC skip for expedience.

---

### One-paragraph summary, if you remember nothing else

Build a `PROPOSER`-only `agent_framework` module. Declare only non-gated `READ_*` capabilities.
Every tool resolves its tenant from the ambient context first and never trusts a model-supplied
value as a DB scope. Read the ledger and the tenant's verification status through the existing
readers (`read_business_context`, direct SQL on `customer_ledger_entries` mirroring
`accounting_lane.py`), never invent a new write path. Never file, send, spend, or mutate — this is
a readiness check, not an action. Prove it with `assert_conforms`, keep the module import-light,
and open your PR against `dev` with an allocated VT-ID.


---

## 7. Boundaries recap (external review, 2026-07-18 — read this LAST, remember it FIRST)

An external reviewer (Codex) audited this kit; these clarifications are BINDING:

1. **A module is NOT a "SubAgent."** Launch proves exactly two brained SubAgents (Sales
   Recovery + Onboarding Conductor). Everything new — Marketing, Finance, Compliance — starts
   as a framework MODULE/tool surface. Graduating a module to a full brained SubAgent has a
   promotion bar (Tier-1-clean measurement + explicit Fazal authorization); do not design for
   it prematurely.
2. **Build + verify is builder-takeable; LIVE ROUTING is CC-owned.** You deliver a registering,
   conformance-passing, unit-tested module on a branch. Wiring it into live dispatch/routing —
   and every deploy — is done by CC after review. Never touch dispatch/triage/routing files.
3. **DB access: wrappers-first, strictly.** New module code reads ONLY through
   `orchestrator.db.wrappers` (or existing sanctioned read helpers). Do NOT open
   `tenant_connection` directly in new code — the direct-connection pattern you may see in older
   lanes is TRANSITIONAL (pre-§7.3 DB-inversion), not a license. If a read you need has no
   wrapper, request one; don't inline SQL.
4. **Conformance proves the SAFETY SHAPE, not competence.** `assert_conforms` green is the
   floor. Your PR must ALSO carry domain evals: fake-injected unit tests for every tool's logic
   and edge cases, and scenario tests for the advice quality. A safe module that gives bad
   business advice fails review.
5. **Structured outputs ONLY — never owner-facing prose.** Your tools return dicts/dataclasses;
   the Manager renders every word the owner reads. A module that returns polished owner copy is
   a shadow conversational agent and will be rejected in review.
6. **Trusted-builder posture.** There is no sandboxing, egress restriction, or dependency
   scanning for modules — this contract is for trusted first-party engagements only, not a
   marketplace. Your code is reviewed as first-party code.
7. **Entitlement is soft-open.** `entitlement_key` is declared but billing does NOT enforce it
   yet — never assume paid activation gates your module's availability.
