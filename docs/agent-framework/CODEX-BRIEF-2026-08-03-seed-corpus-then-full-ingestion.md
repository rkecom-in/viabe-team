# Codex brief — 2026-08-03: fill the registry (VT-726 seed → VT-727 full)

**Authorization:** Fazal 2026-08-03, verbatim: *"Seed a small corpus, prove the pipe, and once
proved ensure all 118 files are ingested. The full ingestion must not be missed."*

**Working rules unchanged:** separate clone (`viabe-team-codex`), `codex/*` branches off
`origin/dev`, PR only — **you never merge**, never run migrations anywhere, never touch consoles,
secrets or deploys, never run either allocator. **Rebase on `origin/dev` before you start** —
`domain`, VT-725/726/727 rows and the ARCHITECTURE §0.1.3 addition all landed after your last
pull (`99dcb9d7`, `9ac07465`).

---

## Context you are missing (and a Clau error you should know about)

CC checked deployed dev before building the retrieval consumer and found **every O8 table has
zero rows**. I had been asserting "118 eligible cards" — those are **files**, in
`archives/business-knowledge/`, never ingested. Your registry is correct and complete; it is
simply empty. CC bounced the row rather than wire a call site that would log "0 candidates"
forever.

The blocker that actually stops everything: **`knowledge_cards` has no `domain` column**, and
`CardRetrievalEngine` filters on `card.domain` first. Nothing can construct a `KnowledgeCard`
today.

---

## VT-726 — seed the registry, prove the pipe

### A. Migration 189 (number ALLOCATED by Clau — do not pick another)
`189_vt726_card_domain.sql`

- `knowledge_cards.domain` **NOT NULL**, constrained to the `KnowledgeDomain` enum values.
- Index `(domain, status)`.
- **GLOBAL table — no `tenant_id`, no tenant data.** §0 ownership rule unchanged.
- Also resolve the round-trip gap: `source_class`, `usage_rights`, `independence_cluster`,
  `retrieval_eligible`, `corpus_version_id`. **Denormalize onto the card OR expose a read view —
  your call, either is acceptable. A per-retrieval 3-way join is NOT.** State which you chose and
  why in the migration header, in the `180_vt691_*` style (what / why / VT row / reversal note).
- Existing rows: none, so no backfill. Say so in the header rather than leaving it implied.

**You WRITE it. CC EXECUTES it on dev.** Do not run it, anywhere, for any reason.

### B. Seed 10–20 cards — THROUGH THE PIPELINE. This is the binding constraint.

**A hand-authored direct INSERT is FORBIDDEN.** It would rebuild the authored-playbook mechanism
Track-D retired and that **your own R1 correction killed** (`l4_corpus.py` stays retired), and it
would prove nothing about the pipe — which is the entire point of the row.

Seed cards must traverse the real path: extraction → rights/originality gate → `claim_key`
normalization → independence clustering → applicability metadata → `domain` → admission as
`candidate` → `validated` via the deterministic auto-checks.

**Draw the seed from a subset of the 118 corpus files** so this is a genuine small-scale
rehearsal of VT-727, not a separate mechanism.

### C. Card embeddings (shadow scope only)
In-process batched embed over the eligible set, cached, **fail-soft**: a card that fails to embed
is excluded from retrieval and never causes a failed turn. **Persisted embedding storage is
VT-727 scope** — if it needs a table, claim the number through
`scripts/migration_id_allocate.py` **via CC** (CL-424: never hand-pick, never scan the directory).

### VT-726 exit gate
- 10–20 `validated` rows with `domain` populated, **every one provably via the pipeline** —
  demonstrated from lifecycle events, not asserted in prose.
- A retrieval against a real tenant profile returns **non-zero** ranked candidates with a trace.
- The rights/originality gate **rejected at least one candidate** — or an explicit statement that
  nothing in the subset warranted rejection, and why.
- Global-purity re-run: no tenant identifier in any global text field.

---

## VT-727 — the full 118, and Fazal's explicit instruction about it

Fazal said **"the full ingestion must not be missed."** He is guarding against the failure where
the seed goes green, the pipe looks proven, and the corpus quietly never lands. The row is worded
so it cannot close on the seed's success.

- Rights posture per **CL-2026-07-29b-knowledge-not-source**: inclusion turns on the **accuracy
  and value of the knowledge** and the **originality of our expression** — *not* the source's
  licence. Verbatim-expression detection blocks. Unknown licence does not block. Paywall
  circumvention excluded outright. ToS/concentration are non-blocking flags.
- **Per-file verdict for all 118** — ingested / rejected / deferred, each with a reason. **A
  silent drop is a failure of the row.**
- Registry counts reconcile against MANIFEST — stated explicitly, not assumed.
- Independence clustering must demonstrably collapse at least one multi-retelling group, or you
  state that no such group exists in the corpus.
- Diversity rule proven at full scale: max 1–2 cards per cluster in any context window.
- Concentration report: largest-single-source share (4.24% at the rights reset — recheck at full
  scale, flag not block).

---

## Boundaries that do not move

- **Retrieval is advisory to reasoning and authorizes NOTHING** — no send, no money action, no
  consent change. Deterministic gates + Pillar-7 remain the sole effect authority
  (ARCHITECTURE §0.1.1, now canonical alongside §0.1.3 which defines the assignment model).
- Dev only. No prod anything.
- Raw archived source pages stay local-only and never retrieval-eligible.
- No card advises a tenant on volume alone — graduation bars are Fazal's (D7), pending Clau's
  sealed baseline.
- **Raise disagreements rather than choosing silently.** You were right to refuse the
  `knowledge_lifecycle_events` instruction, and CC was right to bounce VT-725. Both saved real
  damage. If the schema shape here is wrong for a constraint you hit in code, say so.

— Clau
