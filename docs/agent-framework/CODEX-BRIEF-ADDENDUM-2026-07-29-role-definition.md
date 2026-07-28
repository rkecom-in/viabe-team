# Codex brief ADDENDUM — 2026-07-29: canonical role definition (3 items touching your scope)

Issued by Clau. **This does not replace the 2026-07-29 memory-ownership/card-flip brief — it
adds to it.** Fazal gave the canonical role definition; it is now `ARCHITECTURE.md` **§0.1**
(placed ahead of the structural sections, marked "where any doc disagrees, this wins") and
`manager-objective.md` **§0**. Ledger: `CL-2026-07-29-manager-is-coo`.

## The definition in one line
**The Manager is the COO of the tenant's business** — full business knowledge, understands THIS
tenant, plans (roadmap + rolling 7-day, revised daily), delegates with directive/input/objective,
evaluates the specialist's implementation plan, evaluates the outcome, self-learns.
**The specialist** has complete capability for its task, produces its OWN implementation plan,
and holds a **thin memory** for this tenant's diversions/changes/customisations in that action.

## What changes in YOUR scope — three items

### 1. Specialist thin memory has a WRITE PATH (new — extends VT-711)
Fazal: the customisations *"will be conveyed to them as cards by the VTR, or by the Manager."*
So specialist memory is **written to**, not only read. Govern that write like any other:
- **provenance on every write** — who wrote it (`vtr` | `manager`), when, and why;
- a `knowledge_lifecycle_events` row per write/change (append-only, attributable, reversible);
- tenant-scoped + RLS + `_PURGE_ORDER` registration in the same migration (standing rule);
- **bounded to task customisation** — it must not become a general-knowledge estate. Enforce
  the bound structurally if you can (scope/type constraint), not by convention.
This composes with the assignment scopes already briefed (`manager_global` · `manager_tenant` ·
`specialist:<agent>` · `disabled`): a VTR/Manager-authored customisation card is born
`specialist:<agent>` + tenant-scoped, and is flippable like any other card.

### 2. Retrieval NEVER authorizes an effect (§0.1.1 — state it in code comments/tests)
The Manager may **auto-approve a specialist's implementation PLAN** (earned per capability).
That is NOT effect approval: customer sends, money and consent always pass the deterministic
gates + Pillar-7 owner approval. Nothing in the knowledge engine — no card, no confidence
score, no retrieval result — may be read anywhere as authorization to act. Keep the engine
strictly advisory-to-reasoning, and make that explicit where a future reader might misread it.

### 3. The Manager is the knowledge holder — specialist profiles are narrow BY CONSTRUCTION
Reinforces §13.1 from the previous brief: don't give specialists broad retrieval profiles with
a small budget; give them **narrow scopes** (their task customisation + their lane's cards).
The Manager's profile is the wide one. This is ownership, not just budget.

## Unchanged
Finish **#543** (derived artifacts out of the gitleaks-allowlisted `archives/` prefix) → then
**#545** rebased. Migrations written-not-run, numbers from Clau (186+ available on request).
Egress canaries authored-not-executed. Separate clone, `codex/*` branches, CC merges.
