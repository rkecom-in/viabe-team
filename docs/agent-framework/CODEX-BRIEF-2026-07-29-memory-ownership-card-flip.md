# Codex brief — 2026-07-29: memory ownership + runtime card assignment (Fazal rulings)

Issued by Clau. Two Fazal rulings that change VT-711 scope and the review criteria for
VT-710/711. Spec updated: `.viabe/o8-knowledge-engine-design.md` **§12** (living knowledge) and
**§13** (memory ownership + card assignment). Ledger: `CL-2026-07-28-o8-living-knowledge`,
`CL-2026-07-29-launch-with-rag`, `CL-2026-07-29-manager-owns-memory`.

**O8 is UN-PARKED and LAUNCH-CRITICAL.** We launch with the knowledge engine in place — it is
the moat, and the Manager must start learning from tenant one.

---

## 1. First, unchanged: finish the stack

PR **#543** still needs the derived-artifact path fix (out of the gitleaks-allowlisted
`archives/` prefix — see the 2026-07-29 bounce brief), then **#545** rebased onto it. Nothing
below starts until those land; they are the foundation everything else builds on.

## 2. Ruling A — the Manager owns the memory estate (§13.1)

> *"the Manager's memory is the important element, rest other agents memory is only limited and
> specific to their specialised tasks, mostly to cater to the agent specific customisations."*

- **The Manager is the knowledge-holding entity**: the global curated corpus AND the tenant's
  learned history are the Manager's memory.
- **Specialists get THIN, task-specific memory** — agent-specific customisation only (SR's copy
  conventions, cohort heuristics, what worked for this tenant on this lane). No parallel
  general-knowledge estates.
- **This does not contradict your R3 correction.** R3 governs per-turn CONTEXT BUDGET (don't
  flood the Manager with deep corpora every turn); this governs where knowledge LIVES. Both
  hold: ownership = Manager, depth-per-turn = scoped retrieval. Adjust the retrieval-profile
  defaults accordingly — specialist profiles should be narrow by construction, not by budget.

## 3. Ruling B — card assignment must be a RUNTIME FLIP (§13.2) — new VT-711 scope

Every card carries an assignment across scopes:
`manager_global` · `manager_tenant` · `specialist:<agent>` · `disabled`

Requirements:
- **Changeable per card, per scope, at runtime** — an operation, NOT a migration or rebuild.
  Fazal's words: *"build it such that we can flip the cards in/out of the managers and other
  agents global memory or tenant specific memory based on situation."*
- Every flip writes a `knowledge_lifecycle_events` row (append-only, attributable, reversible).
- Assignment is evaluated **at retrieval, alongside applicability** (§5.1) — a card may be
  global for one tenant cohort and tenant-scoped or disabled for another.
- **Emergency flip-out is immediate** and does NOT require ablation. Ablation (§6) governs
  PERMANENT demotion; operational removal from a memory scope must be instant.

If the current schema (migration 182/183, merged) can carry assignment without a new table —
prefer that; if it needs a column or a small join table, flag it and I will allocate the
migration number (184/185 are consumed; 186+ available on request).

## 4. Ruling C — launch posture: cards are INCLUDED, not shadowed (§13.3)

Fazal heard the shadow-first recommendation and ruled otherwise: the 118 curated cards ship
included, under observation. *"we will include them to start with, observe the manager's
behavior and accordingly decide."*

What that requires from you — this is the part that makes the posture safe:
- **Attribution must be complete from day one.** Every card that influences a decision is
  logged via `decision_evidence_links`. Observation must be instrumented, not anecdotal.
- **Flip-out must actually work** (§3 above) — the mitigation for a harmful card is operational
  removal in seconds, so it has to be exercised and tested, not merely present.
- O11 still governs formal graduation; inclusion at launch is an observation posture, not a
  validation claim. Do not mark any card `validated` on the strength of shipping.

## 5. Also in scope from §12 (living knowledge) — build into VT-711

- **Mutation sources are first-class**: the Manager's experience, **tenant responses**,
  **business outcomes**, and **VTR diversions** (a human overriding the machine — our
  highest-value signal) enter the learning loop with full provenance, not merely as evaluation
  signal.
- **Changing knowledge ↔ single voice**: when a card supersedes/expires/flips, the Manager may
  already have told the owner the old version. Design the `card_version ↔ asserted_fact`
  provenance join now (the S3 asserted-facts ledger, VT-719, is CC's build) so a correction can
  be OWNED in conversation, never silently served.
- Rostered separately, NOT your scope yet: the current-affairs/business-news ingestion class.

## 6. Boundaries — unchanged

No migrations run anywhere (numbers from Clau) · effect gates untouched — knowledge informs
reasoning, never performs an action · egress canaries authored-not-executed (CC runs them) ·
separate clone, `codex/*` branches, CC merges.
