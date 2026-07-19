# `agent_framework` — build + verify an agent module

This package is the **modular agent-integration contract**. A module is a small, self-contained
class that **PROPOSES** and/or **EXECUTES**; the platform **ENFORCES** every trust gate. A module
depends on **ONLY** the `orchestrator.agent_framework` public surface — nothing submodule-deep,
nothing in the live orchestrator/coordinator. That single dependency is what makes a module
**independently developable and Codex-takeable**: you can write, test, and verify it against this
package alone, and hand it off, without reading the rest of the codebase.

The framework is **additive and inert**: importing it wires nothing, registers nothing, and changes
no routing. A module only becomes live through a deliberate, separate wiring step.

---

## The two roles

Every agent is one or both of:

- **`PROPOSER`** — a conversational-lane module. Reads context, returns a **proposal** (an intent /
  draft / recommendation). **No side effects.** Its `GateFacade` is empty of gated capabilities —
  it *structurally cannot* send or spend.
- **`EXECUTOR`** — a coordinator-dispatched module. Does work against a claimed work item and **arms
  consequential actions only through the `GateFacade`**, which routes to the platform's existing
  deterministic gates.

A module declares a **set** of roles (`roles: frozenset[AgentRole]`, min 1). The Sales-Recovery
shape is **one module that is BOTH** — it proposes in the chat lane and executes a work item. It
registers **once**; `RegisteredModule.run` dispatches to `propose` under a proposer context and
`execute` under an executor context. Even for a dual-role module, the **proposer lane's facade
strips gated capabilities** — a proposal never has a side effect, regardless of what the executor
lane declares.

---

## Build + verify a module in 5 steps

### 1. Write the class with a `manifest` + `propose`/`execute`

```python
from orchestrator.agent_framework import (
    AgentManifest, AgentRole, Capability,
    ModuleContext, ModuleResult, GateFacade,
)

class SalesRecoveryModule:
    manifest = AgentManifest(
        name="sales_recovery",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),   # ONE module, BOTH roles
        description="Proposes lapsed-customer win-back in chat; executes the send as a work item.",
        capabilities=frozenset({
            Capability.READ_CUSTOMER_LEDGER,      # non-gated read
            Capability.REQUEST_CUSTOMER_SEND,     # gated — legal because EXECUTOR is a role
        }),
    )

    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        # conversational lane: read + return a PROPOSAL. `gate` is empty of gated caps here.
        return ModuleResult(role=AgentRole.PROPOSER, status="completed",
                            proposal={"drafts": [...]})

    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        # coordinator lane: do the work, ARM the send through the facade (never a raw transport).
        gate.request_customer_send(draft_id, autonomy_level="L2")
        return ModuleResult(role=AgentRole.EXECUTOR, status="sent", work_item_status="sent")
```

A pure proposer declares `roles=frozenset({AgentRole.PROPOSER})` and implements only `propose`; a
pure executor declares `{AgentRole.EXECUTOR}` and implements only `execute`.

### 2. Declare capabilities **positively** (gated ⇒ EXECUTOR, serviced by the facade)

`capabilities` is the **upper bound** on what the module may do. Two families:

- **Non-gated** (`READ_*`, `PROPOSE_*`) — reads and proposals; no side effect.
- **Gated** (`REQUEST_*`) — a *request* to run a consequential action. A gated capability is legal
  **only if `EXECUTOR` is a declared role**, and it is **serviced only by a `GateFacade` method**
  that routes to a real deterministic gate. There is **no capability that means "send directly."**

A pure `PROPOSER` declaring a gated capability is **rejected** at validation.

### 3. Reach every side effect **ONLY** via the `gate` facade

The module never imports `customer_send` / `twilio` / `business_impact_choke`. The `GateFacade` it
receives is **capability-scoped from its manifest** and **tenant-pinned** (IDOR-resolved) — a gated
method the manifest did not declare raises `CapabilityNotDeclared`, and the module can never act for
another tenant.

### 4. `register_agent(MyModule())`

```python
from orchestrator.agent_framework import register_agent
register_agent(SalesRecoveryModule())
```

Registration validates the manifest, checks the tool surface against the deny-list, and confirms the
impl exposes the method for **each** declared role. (Registration is inert — it does not wire the
module into any live seam.)

### 5. Run `assert_conforms(MyModule())` + `pytest`

```python
from orchestrator.agent_framework import assert_conforms

def test_sales_recovery_conforms():
    assert_conforms(SalesRecoveryModule())   # fails the test at the first contract violation
```

`assert_conforms` runs the **conformance suite** — the single verification process for ANY module.
For a structured, non-raising report (CI gating, diffing compliance over time) use
`check_module_conformance(module) -> ConformanceReport`. The checks:

| check | asserts |
|---|---|
| `has_manifest` | module exposes an `AgentManifest` |
| `manifest_valid` | `manifest.validate()` passes |
| `capabilities_legal_for_roles` | every capability legal for the roles (gated ⇒ EXECUTOR) |
| `tool_surface_safe` | tool surface passes the deny-list |
| `role_methods_present` | a callable method exists for each declared role |
| `proposer_gate_readonly` | a proposer's facade raises `CapabilityNotDeclared` on every gated method |
| `gated_capabilities_serviced` | every declared gated capability has a real facade method (no orphan) |
| `name_registerable` | non-empty name; registers cleanly into a fresh registry |
| `required_tools_reachable` | every `tools=(...)` entry exists in the generated tool catalog |
| `brief_complete` | VT-686 identity card complete: `category` ∈ `AGENT_CATEGORIES`, non-empty `tags`, every `AgentBrief` field filled |

**Conformance vs registration:** `register_agent()` runs only `manifest_valid` + `tool_surface_safe`
+ `role_methods_present` (permissive by design — a staged/incomplete module may register; it is
silently omitted from the Manager's agent directory until its card is complete). The full 10-check
suite is enforced in tests (`assert_conforms`) AND at process boot for every first-party module
(`modules.register_all_modules` raises `ModuleRegistrationError` on the first violation — boot
fails loudly, never a silently card-less agent).

---

## The public surface a module depends on

Import **everything** from `orchestrator.agent_framework` (never a submodule-deep path). The full
SDK is its `__all__`: `AgentManifest`, `AgentRole`, `Capability` / `GATED_CAPABILITIES` / `is_gated`,
`AgentBrief` / `AGENT_CATEGORIES` (the VT-686 identity-card taxonomy: `category` must be one of the
closed `AGENT_CATEGORIES` set; `tags` lowercase free-form; `brief` the five-field `AgentBrief` the
Manager's agent directory renders),
`ModuleContext` / `ModuleResult` / `TenantResolutionError`, `GateFacade` / `CapabilityNotDeclared`,
`ProposerModule` / `ExecutorModule`, `register_agent` / `register_activation_prereqs`,
`check_module_conformance` / `assert_conforms` (+ `ConformanceReport` / `CheckResult`),
`check_entitlement`, and the registry/error types. **This surface is the entire contract** — if it
compiles against `agent_framework` and `assert_conforms` passes, the module is integration-ready.

### Activation bar (optional)

A module may carry an activation `prerequisites=AgentPrerequisites(agent=name, ...)`. It is the
**single source** of that module's bar. Publishing it into the live activation gate is a deliberate,
explicit step — `register_activation_prereqs(MyModule())` — never done at import.

### Entitlement (billable modules)

`entitlement_key` on the manifest is a **self-describing SKU** ("this agent is billable"). Whether a
tenant may run it is **COMPUTED from billing** (in-trial OR active-paid) via `check_entitlement`,
which is **SOFT** — it never hard-blocks and never encodes a price. It is **soft-open (returns
`True`) until billing matures**; the metering read wires in at activation.
