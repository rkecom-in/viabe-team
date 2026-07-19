# Build the Sales Recovery agent as a module — from a fresh Mac

A plain, from-zero walkthrough of how you'd develop the **Sales Recovery** agent as a self-contained
module against Viabe's `agent_framework` contract. Grounded in the actual package
(`apps/team-orchestrator/src/orchestrator/agent_framework/`), not a sketch.

---

## 0. What you're building, in one paragraph

A "module" is **one small Python class** that (a) **declares** what it needs (a *manifest*), (b)
**proposes** a win-back campaign in chat, and (c) **executes** the owner-approved send. It never
touches the send/spend machinery directly — it can only *request* an action through a **gate facade**,
which routes through the platform's existing consent/budget/approval gates and pins it to one tenant.
You write, test, and verify it **in isolation** against the `agent_framework` package alone.

### The safety wall (the mental model)

```
   your module  ──proposes──▶  (proposer lane: NO power, cannot send/spend)
        │
        └──executes──▶  GATE FACADE  ──▶  existing gates (consent · budget · owner-approval · onboarded)
                        the one locked door        the same rails the manager already uses
   • only services capabilities your manifest declared
   • tenant-pinned — can never act for another customer
   • adds no gate, bypasses none
```

**What the framework enforces today (BUILT):** a module can't reach any *action* it didn't declare,
can't send/spend except through the real gates, can't act for another tenant.
**What it does NOT do yet (NOT BUILT):** it does not sandbox your code or scan it. So this safely
enables **our own + Codex-built modules (trusted code)** — running *untrusted third-party* code needs
a sandboxing/scanning/review layer that isn't built.

---

## 1. Set up a fresh Mac (the tools)

Open **Terminal** (Cmd-Space → "Terminal"). Run these in order.

**1.1 Homebrew** — the macOS package manager (installs everything else):
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
After it finishes, follow the "Next steps" it prints (adds `brew` to your PATH).

**1.2 git + GitHub CLI** (the repo is private, so you need auth):
```bash
brew install git gh
gh auth login          # choose GitHub.com → HTTPS → login in browser
```

**1.3 uv** — the Python package/environment manager this repo uses (also installs Python for you):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Close and reopen Terminal so `uv` is on your PATH.

**1.4 An editor** (recommended, optional):
```bash
brew install --cask visual-studio-code
```
Open VS Code → Extensions → install **Python** (by Microsoft).

> That's the whole toolchain. **You do NOT need** Postgres, Docker, Twilio keys, or an LLM key to
> **build and verify a module** — the contract is dependency-light and modules unit-test with injected
> fakes (that's the whole point of the isolation). You'd only need those to run the full orchestrator.

---

## 2. Get the code

```bash
gh repo clone rkecom-in/viabe-team        # needs your account added to the private repo
cd viabe-team/apps/team-orchestrator
uv python install 3.13                    # the repo runs on Python 3.13
uv sync                                    # creates the venv + installs dependencies
```

Read the two files that ARE the contract (5 minutes):
- `src/orchestrator/agent_framework/README.md` — the 5-step build guide + the SR example.
- `src/orchestrator/agent_framework/reference_plugin.py` — the canonical working module to copy.
- `src/orchestrator/agent_framework/capabilities.py` — the menu of what you can declare (below).

---

## 3. Know the menu — what data/actions you can declare

A module declares only from this fixed catalog (`Capability`). Anything not declared, it cannot do.

| Family | Capability | Meaning |
|---|---|---|
| Read (no effect) | `READ_CUSTOMER_LEDGER` | who bought / who's lapsed |
| Read | `READ_BUSINESS_CONTEXT` | the owner's goal + business identity |
| Read | `READ_INTEGRATION_STATE` | connector status |
| Propose (no effect) | `PROPOSE_CAMPAIGN` / `PROPOSE_DRAFT` / ... | hand back an intent/draft, never executed |
| **Gated (EXECUTOR only)** | `REQUEST_CUSTOMER_SEND` | ask the platform to send a draft → routes to Gate 0..5 |
| **Gated (EXECUTOR only)** | `REQUEST_BUSINESS_ACTION` | ask to run a spend/commitment → routes to the impact gate |

There is **no capability that means "send directly."** The strongest thing you can declare is "*ask*
the platform, through the facade, to run a gated action" — and the platform still decides
autonomous-vs-owner-approval.

---

## 4. Write the module

Create `src/orchestrator/agent_modules/sales_recovery_module.py`:

```python
from __future__ import annotations
from collections.abc import Callable
from typing import Any

from orchestrator.agent_framework import (
    AgentManifest, AgentRole, Capability,
    ModuleContext, ModuleResult, GateFacade,
)

# Injectable fakes → the module unit-tests with NO database (the reference-plugin convention).
LapsedReaderFn = Callable[[str], list[dict[str, Any]]]     # tenant_id -> lapsed rows
DraftForItemFn = Callable[[str | None], str]               # work_item_id -> approved draft_id


class SalesRecoveryModule:
    """Proposes a lapsed-customer win-back in chat; executes the owner-approved send."""

    manifest = AgentManifest(
        name="sales_recovery",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER, AgentRole.EXECUTOR}),  # ONE module, BOTH roles
        description="Proposes a win-back to lapsed customers; executes the owner-approved send.",
        capabilities=frozenset({
            Capability.READ_CUSTOMER_LEDGER,     # non-gated read — who is lapsed
            Capability.PROPOSE_CAMPAIGN,         # non-gated  — hand back a campaign draft
            Capability.REQUEST_CUSTOMER_SEND,    # GATED — legal ONLY because EXECUTOR is a role
        }),
    )

    def __init__(self, *, lapsed_reader: LapsedReaderFn | None = None,
                 draft_for_item: DraftForItemFn | None = None) -> None:
        self._lapsed_reader = lapsed_reader
        self._draft_for_item = draft_for_item

    # ---- PROPOSER lane: conversational. Read + return a PROPOSAL. No side effect. ----
    def propose(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        lapsed = (self._lapsed_reader or _real_lapsed)(str(ctx.tenant_id))
        return ModuleResult(
            role=AgentRole.PROPOSER,
            status="completed",
            proposal={
                "campaign": "winback",
                "cohort_size": len(lapsed),                    # honest: exactly who we'd reach
                "message": "We miss you — 10% off your next order.",
            },
        )
        # NOTE: `gate` here is EMPTY of gated caps — calling gate.request_customer_send(...) in a
        # proposer would raise CapabilityNotDeclared. A proposal is structurally incapable of a send.

    # ---- EXECUTOR lane: the coordinator dispatches this on the owner-APPROVED work item. ----
    def execute(self, ctx: ModuleContext, gate: GateFacade) -> ModuleResult:
        draft_id = (self._draft_for_item or _real_draft_for_item)(ctx.work_item_id)
        # The ONLY door to a send — routes to customer_send Gate 0..5 (consent/budget/etc.), tenant-pinned.
        gate.request_customer_send(draft_id, autonomy_level="L2")
        return ModuleResult(role=AgentRole.EXECUTOR, status="sent", work_item_status="sent")


# Real reads — lazy-imported so they only load when NOT injected (unit tests stay dependency-free).
def _real_lapsed(tenant_id: str) -> list[Any]:
    from orchestrator.db.wrappers import CustomersWrapper      # real wrapper
    # Real API: returns a list of `LapsedCandidate` DATACLASSES (not dicts). `count_lapsed(...)` also exists.
    # The dict-shaped fake injected in the test below is just your own test shape — that's fine.
    return CustomersWrapper().lapsed_candidates(tenant_id)

def _real_draft_for_item(work_item_id: str | None) -> str:
    ...  # resolve the approved draft from the claimed coordinator work item (illustrative)
```

Two rules the contract enforces for you (you can't get them wrong without a validation failure):
1. `REQUEST_CUSTOMER_SEND` is legal only because `EXECUTOR` is in `roles`. A pure proposer declaring it
   is **rejected** at registration.
2. The module never imports `customer_send` / `twilio`. Its only door is the `gate` it's handed.

---

## 5. Verify it (this is the whole verification process)

Create `tests/agent_modules/test_sales_recovery_module.py`:

```python
from orchestrator.agent_framework import assert_conforms, ModuleContext, GateFacade
from orchestrator.agent_modules.sales_recovery_module import SalesRecoveryModule

def test_conforms():
    # ONE call proves: manifest valid · capabilities legal for the roles · no forbidden tools ·
    # proposer facade is read-only (gated calls raise) · every gated cap has a real door · IDOR-safe.
    assert_conforms(SalesRecoveryModule())

def test_proposal_is_honest_and_read_only():
    mod = SalesRecoveryModule(lapsed_reader=lambda t: [{"id": 1}, {"id": 2}, {"id": 3}])
    # for_proposer parses the UUID directly when there's no ambient dispatch context (i.e. in a unit test).
    ctx = ModuleContext.for_proposer(tenant_model_value="00000000-0000-0000-0000-000000000001",
                                     module_name="sales_recovery")
    # A proposer's gate is EMPTY of gated caps — build it directly with no capabilities. Any
    # gate.request_customer_send(...) here would raise CapabilityNotDeclared (structurally can't send).
    empty_gate = GateFacade(tenant_id=ctx.tenant_id, capabilities=frozenset())
    res = mod.propose(ctx, empty_gate)
    assert res.proposal["cohort_size"] == 3               # honest count, no fabrication
```

Run it:
```bash
uv run pytest tests/agent_modules/test_sales_recovery_module.py -q
```

`assert_conforms` proves **contract-compliance + safety** (the structural checks). Your own tests, with
injected fakes, prove the **business logic** — no database, no keys. If both pass, the module is
integration-ready.

---

## 6. Register it (optional, still inert)

```python
from orchestrator.agent_framework import register_agent
register_agent(SalesRecoveryModule())   # validates + adds to a registry — wires nothing live
```

---

## 7. What is NOT part of "build a module" (honest scope)

Building + verifying a module (steps 4–6) is **fully decoupled and Codex-takeable** — that's what CC
built. These are **separate** and not done by writing the module:

- **Making it live** — routing the manager to your module and dispatching a real work item is a
  deliberate, separate wiring step (the SR/Integration *migration*, which CC owns).
- **Deploying** — needs the full stack (Postgres, Twilio, LLM keys, Railway/Vercel) — not needed to build.
- **Third-party safety** — for *untrusted* code you'd first need the missing layer: code sandboxing,
  malware/dependency scanning, a security review, and a submission/marketplace portal. Today the contract
  makes third-party agents *admissible in principle*; it does not yet make running arbitrary third-party
  code *safe*.

---

*Reference: `src/orchestrator/agent_framework/{README.md, capabilities.py, manifest.py, gate_facade.py,
context.py, conformance.py, registration.py, reference_plugin.py}`. Framework = VT-649/650, additive + inert.*
