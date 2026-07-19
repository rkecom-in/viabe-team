# Agent Framework — complete reference

**The authoritative doc for building, verifying, and integrating a Viabe agent module.** Written for
anyone (a Viabe engineer, Codex, or a future third party) building a specialist agent against the
`agent_framework` contract. Every path and name here is from the shipped package
(`apps/team-orchestrator/src/orchestrator/agent_framework/`, VT-649 + VT-650).

> Status: this doc's **Build + Verify** sections are validated against the shipped code. The **Integrate
> (wire-to-live)** section (§8) is CC-owned (not builder-takeable) — documented from the SR/Integration migration as
> that lands (CC owns the migration). Everything else is inert-and-stable today.

---

## 1. What this is (the mental model)

A **module** is one small Python class that **PROPOSES** and/or **EXECUTES**, and the platform
**ENFORCES** every trust gate. A module depends on **only** the `orchestrator.agent_framework` public
surface — nothing deeper — which is what makes it independently developable and handoff-ready.

```
   module.propose(ctx, gate)  ──▶  a PROPOSAL (draft/recommendation). NO side effect. gate is empty.
   module.execute(ctx, gate)  ──▶  gate.request_customer_send(...) ─▶ EXISTING gates ─▶ effect
                                    the ONE locked door             consent · budget · approval · onboarded
```

The `GateFacade` is the **trust boundary**: capability-scoped from the manifest, tenant-pinned
(IDOR-safe), routing to the platform's existing deterministic gates — it adds no gate and bypasses none.
A module can never send/spend/cross-tenant except through it.

**Guarded today:** *actions* (a module can't reach an undeclared action, can't send/spend except via the
real gates, can't act for another tenant). **NOT guarded:** arbitrary *code* — nothing sandboxes or scans
a module. So this safely enables **first-party + Codex-built modules** (trusted code); running *untrusted
third-party* code needs the layer in §11. The framework is **additive and inert** — importing it wires
nothing; a module goes live only via the explicit steps in §8.

---

## 2. Where it lives — file map

Package root: `apps/team-orchestrator/src/orchestrator/agent_framework/`

| File | Purpose | Key exports |
|---|---|---|
| `__init__.py` | The **public SDK surface** — the single import point. A module imports ONLY from here. | the full `__all__` (see §3) |
| `manifest.py` | The declaration form a module carries. | `AgentManifest` (`.validate()`, `.as_prerequisites()`) |
| `capabilities.py` | The roles + the fixed capability catalog. | `AgentRole`, `Capability`, `GATED_CAPABILITIES`, `is_gated`, `ROLE_METHOD` |
| `context.py` | The input/output value objects. | `ModuleContext` (`.for_proposer`/`.for_executor`), `ModuleResult`, `TenantResolutionError` |
| `gate_facade.py` | **The trust boundary** — the one door to gated actions. | `GateFacade` (`.request_customer_send`, `.gate_business_action`), `CapabilityNotDeclared` |
| `registration.py` | Validates + registers a module (inert — wires nothing live). | `register_agent`, `register_activation_prereqs`, the registry |
| `conformance.py` | **The verification suite.** | `assert_conforms`, `check_module_conformance`, `ConformanceReport`, `CheckResult` |
| `entitlement.py` | Billable-module entitlement (soft, computed-from-billing). | `check_entitlement` |
| `protocols.py` | The structural typing for the two roles. | `ProposerModule`, `ExecutorModule` |
| `reference_plugin.py` | The canonical worked example (a read-only proposer). | `BusinessContextReader` |
| `README.md` | In-package quick-start (the 5-step guide). | — |

Related (used, not part of the contract): `docs/agent-framework-build-sales-recovery.md` (from-scratch
tutorial, incl. fresh-Mac setup); `tests/agent/test_agent_framework.py` (the framework's own tests).

---

## 3. The public API (the SDK surface)

Import **everything** from `orchestrator.agent_framework` — never a submodule-deep path. The full
contract is its `__all__`:

- **Declaration:** `AgentManifest`, `AgentRole`, `Capability`, `GATED_CAPABILITIES`, `is_gated`
- **Context:** `ModuleContext`, `ModuleResult`, `TenantResolutionError`
- **Trust boundary:** `GateFacade`, `CapabilityNotDeclared`
- **Typing:** `ProposerModule`, `ExecutorModule`
- **Registration:** `register_agent`, `register_activation_prereqs`
- **Verification:** `assert_conforms`, `check_module_conformance`, `ConformanceReport`, `CheckResult`
- **Entitlement:** `check_entitlement`

If your module compiles against `agent_framework` and `assert_conforms` passes, it is integration-ready.

---

## 4. The capability catalog + roles

**Roles** (`AgentRole`): `PROPOSER` (conversational; returns a proposal; NO side effects) and `EXECUTOR`
(coordinator-dispatched; arms actions only through the facade). A module declares a **set** (min 1);
Sales Recovery is one module declaring BOTH.

**Capabilities** (`Capability`) — declare only from this fixed menu; anything not declared is impossible:

| Family | Capability | Effect |
|---|---|---|
| Read | `READ_CUSTOMER_LEDGER` | who bought / who's lapsed |
| Read | `READ_BUSINESS_CONTEXT` | the owner's goal + business identity |
| Read | `READ_INTEGRATION_STATE` | connector status |
| Propose | `PROPOSE_CAMPAIGN` / `PROPOSE_DRAFT` / `PROPOSE_CONFIG_CHANGE` / `PROPOSE_BUSINESS_ACTION` | hand back an intent/draft; never executed |
| **Gated** (EXECUTOR only) | `REQUEST_CUSTOMER_SEND` | ask to send a draft → routes to `customer_send` Gate 0..5 |
| **Gated** (EXECUTOR only) | `REQUEST_BUSINESS_ACTION` | ask to run a spend/commitment → routes to the impact choke |

**No capability means "send directly."** The strongest thing a module can declare is "ask the platform,
through the facade, to run a gated action" — and the platform still decides autonomous-vs-approval. A
pure `PROPOSER` that declares a gated capability is **rejected** at registration. Adding a new capability
is a deliberate reviewed code change (new gated cap ⇒ also add to `GATED_CAPABILITIES` + give it a
`GateFacade` method that routes to a real gate).

---

## 5. Build a module

Full from-scratch walkthrough (incl. a fresh-Mac toolchain: Homebrew → git/gh → uv → `uv sync`, then the
Sales Recovery example): **`docs/agent-framework-build-sales-recovery.md`**.

The shape (see `reference_plugin.py` for the minimal real one):

```python
from orchestrator.agent_framework import (
    AgentBrief, AgentManifest, AgentRole, Capability, ModuleContext, ModuleResult, GateFacade,
)

class MyModule:
    manifest = AgentManifest(
        name="my_agent", version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),
        description="...",
        capabilities=frozenset({Capability.READ_CUSTOMER_LEDGER, Capability.REQUEST_CUSTOMER_SEND}),
        # VT-686 identity card — REQUIRED for a directory-visible agent (conformance #10
        # brief_complete; also enforced at BOOT for every first-party module):
        category="Sales",                       # ONE of AGENT_CATEGORIES (closed set)
        tags=frozenset({"winback", "lapsed"}),  # lowercase, free-form
        brief=AgentBrief(
            what_it_does="Wins back lapsed customers with owner-approved campaigns.",
            actions=("draft_campaign", "arm_approval"),
            business_activities=("win back lapsed customers",),
            when_to_use="Route here when the owner asks about lapsed/dormant customers.",
            limits=("never sends directly — arms the approval gate",),
        ),
        # optional: prerequisites=AgentPrerequisites(...), tools=(...), entitlement_key="..."
    )
    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult: ...   # no side effect
    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:       # arms via gate only
        gate.request_customer_send(draft_id, autonomy_level="L2")
        return ModuleResult(role=AgentRole.EXECUTOR, status="sent", work_item_status="sent")
```

**Registration vs conformance (know the difference):** `register()` is deliberately permissive —
it runs the structural manifest validate + the tool deny-list only, so a module with a defaulted
`category`/`tags`/`brief` still registers; it is just silently omitted from the Manager's agent
directory. The FULL 10-check suite (incl. `brief_complete`) runs (a) in your tests via
`assert_conforms`, and (b) at process BOOT for every first-party module (`register_all_modules`
crashes boot loudly on the first violation). A drop-in module that skips its identity card is
invisible to the Manager — complete the card.

Rules the contract enforces for you: a gated capability is legal only with the `EXECUTOR` role; the
module never imports `customer_send`/`twilio` (its only door is the injected `gate`); it never picks its
own tenant (`ctx.tenant_id` is IDOR-resolved).

---

## 6. Verify (the single process)

```python
from orchestrator.agent_framework import assert_conforms
assert_conforms(MyModule())        # raises on the first violation
# or, for a non-raising report (CI/diffing): check_module_conformance(MyModule()) -> ConformanceReport
```

The 10 checks: `has_manifest`, `manifest_valid`, `capabilities_legal_for_roles` (gated ⇒ EXECUTOR),
`tool_surface_safe` (deny-list), `role_methods_present`, `proposer_gate_readonly` (a proposer's facade
raises on every gated call), `gated_capabilities_serviced` (no orphan gated cap), `name_registerable`,
`required_tools_reachable` (VT-669 sufficiency: every manifest-required tool exists in the catalog),
`brief_complete` (VT-686: category ∈ AGENT_CATEGORIES, ≥1 tag, a full AgentBrief incl. honest limits —
the Manager-facing identity card every module must carry).
Verifying a **trusted, reviewed** module is "run this suite" — conformance proves the safety SHAPE,
not competence, and is NOT a substitute for review or sandboxing of untrusted code. Test business logic with
**injected fakes** (no DB, no keys) — see the reference plugin's `reader=` and the tutorial's test.

---

## 7. Entitlement (billable modules)

`entitlement_key` on the manifest is a **self-describing SKU** ("this agent is billable"). Whether a
tenant may run it is **computed from billing** (in-trial OR active-paid) by `check_entitlement`, which is
**soft** (never hard-blocks, never encodes a price) and **soft-open until billing matures**. It never
hardcodes ₹5,000 — the billing store is the single source. (CL-2026-07-15-entitlement-computed.)

---

## 8. Integrate — wire a module to live  *(CC-OWNED — not builder-takeable)*

**The rule (Codex review 2026-07-18):** steps 1–7 (build + verify) are what an external builder
delivers — a registering, conformance-passing, tested module on a branch. THIS step — routing a
module into live dispatch, flipping flags, deploying — is CC's alone, after review. The SR +
Integration migrations (VT-658/659, complete + delta-gated on dev) are the worked precedent for
what wiring involves; their adapters are the reference. A builder PR that touches
dispatch/triage/routing files fails review by policy.

Wiring has TWO distinct legs (VT-686 live wiring, 2026-07-19 — do not conflate):
1. **REGISTRATION (visibility)** — `agent_framework.modules.register_all_modules()` runs at BOOT
   (main.py, register-before-launch): every first-party module's manifest is validated
   fail-closed and its identity card becomes visible to the Manager's agent directory from the
   first turn. CC adds a new module to this list at merge — that alone makes it DISCOVERABLE
   (the Manager can describe it and route asks toward it honestly).
2. **ROUTING (execution)** — supervisor/coordinator wiring decides what actually EXECUTES.
   Registration never changes routing; a registered-but-unrouted module is honestly described
   as advisory/not-yet-live via its brief + the capability registry.

Building + verifying (§5–6) is fully decoupled and Codex-takeable. Making a module **live** is a
deliberate, separate set of steps CC owns, documented here as the migration lands:

- `register_agent(MyModule())` — validate + add to the registry (inert).
- `register_activation_prereqs(MyModule())` — publish its activation bar into the live gate (explicit,
  never at import).
- Coordinator/manager wiring — how the reactive manager routes to the proposer lane and dispatches the
  executor work item (documented from the actual migration; not yet finalized).

**Do not treat a module as live because it registers.** Registration wires nothing; only these steps do.

---

## 9. Security model (honest)

- **BUILT — the wall guards ACTIONS:** a gated action happens only through the facade → the real gates;
  capability allow-list (a proposer can reach nothing gated); tenant-pinned (IDOR-safe); tool deny-list.
- **NOT built — the wall does NOT contain arbitrary CODE:** a module runs ordinary Python in-process;
  nothing sandboxes it or restricts network egress. Untrusted third-party code could misuse the DATA it's
  allowed to READ (e.g., exfiltrate). No malware/dependency scan, no security review, no isolated
  execution.
- **Bottom line:** safe today for **first-party + Codex-built** modules (trusted code we review). Running
  **untrusted third-party** code needs §11 first.

---

## 10. Conventions

- One coherent module per agent; declare capabilities **positively** (least privilege).
- Inject readers/writers (`reader=`) so unit tests run DB-free (the connector transport-injection pattern).
- Keep the import surface dep-light (lazy-import heavy deps inside methods) so the dep-less smoke suite
  can collect the module.
- Adding a capability or a gated door is a reviewed change in `capabilities.py` + `gate_facade.py`.

---

## 11. NOT built yet (needed for a real third-party ecosystem)

External SDK package · submission/listing portal · marketplace/registry for external modules · **code
sandboxing** (untrusted-code containment) · malware/dependency scanning · security-review pipeline. Until
these exist, the contract makes third-party agents *admissible in principle*, not *safe to run as
arbitrary code*.

---

## 12. For Codex / a new author — the checklist

1. Read this doc + `agent_framework/README.md` + `reference_plugin.py`.
2. Follow `docs/agent-framework-build-sales-recovery.md` (setup → write → verify).
3. Declare only the capabilities you need (§4); gated ⇒ you need the `EXECUTOR` role.
4. Reach every side effect through the `gate` — never a direct import.
5. `assert_conforms(YourModule())` + your own fake-injected logic tests must pass.
6. Hand off — a reviewer runs `assert_conforms` to accept it. Going live (§8) is a separate CC-owned step.

*Framework: VT-649 (contract) + VT-650 (dual-role, conformance harness, SDK boundary, entitlement/activation). Additive + inert.*
