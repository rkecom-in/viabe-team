# Codex — O8 Knowledge Intelligence: full-program build brief

**Grant: Fazal 2026-07-27** — "Codex should complete all the tasks on implementing the
intelligence as per the plan. CC need not interfere; once Codex is done, CC and Clau review."
**Scope of this grant: BUILD the whole engine INERT. It is not authorization to activate,
deploy, migrate, or serve anything to a tenant.**
Specs of record: `.viabe/o8-knowledge-engine-design.md` v0.2 (the seven Codex corrections are
incorporated) + your own implementation plan (WP0–WP10) + `ARCHITECTURE.md` + this brief.
Working model: `docs/agent-framework/CODEX-WORKING-BRIEF.md` (separate clone, `codex/*`
branches, PRs into dev, CC merges — unchanged).

---

## 1. What changes with this grant

- You own WP2–WP10 build (previously split). **CC is OFF the O8 lane** except: merging your
  checkpoint PRs, and the sealed-baseline run when Clau hands it over.
- **Sealed-set custody moves to Clau** (not CC, not you). You never author, read, or infer the
  held-out set — you authored the knowledge corpus, so this separation is now load-bearing,
  not procedural. Your harness's `assert_sealed_dataset_external` stays the enforcement.
- Rows: **VT-709** (checkpoint A: contracts + registry) · **VT-710** (checkpoint B: ingestion +
  retrieval) · **VT-711** (checkpoint C: learning + admission + rollout wiring). VT-706/707
  fold into A and B. Clau allocates all IDs and migration numbers — you never run either
  allocator.

## 2. Three checkpoints, not one delivery

Land in three reviewable PRs. Rationale: nine tables + RLS/DSR + a retrieval engine reviewed
as one monolith is reviewed worst exactly where it matters most (privacy, isolation).

| Checkpoint | Row | Contents | Review |
|---|---|---|---|
| **A** | VT-709 | Contracts (SourceClass, KnowledgeDomain, CardStatus, Applicability, UsageRights, claim_key/claim_value, independence-cluster, card+corpus versions, taint/lifecycle) · `ModuleResult` evidence/conflict/knowledge_version/grounding_status + adapter preservation · agent retrieval profiles · the 9-table registry schema + migrations (WRITTEN, NOT RUN) + RLS/FORCE-RLS + DSR/_PURGE_ORDER registration + retention + PII-redaction-before-embedding | CC merges; Clau audits vs spec §2/§9 |
| **B** | VT-710 | Ingestion pipeline (acquire → rights → hash/dedupe → quarantine raw → tool-less extraction → schema validation → normalize → redact → cluster → embed → candidates) · retrieval engine (the 12-step order, hard applicability filter before ranking, hybrid semantic/lexical/entity, claim-scoped authority, cluster dedup, diversity cap, minimum-score no-result, per-identity budgets, Manager-vs-specialist boundary) · broker adapter | CC merges; Clau audits vs §3/§5/§7 |
| **C** | VT-711 | Learning loop (L1/L2 tenant; k≥10-gated + contribution-capped + differencing-checked L3 priors) · admission machinery (corpus versions, A/B harness hooks, card ablation) · rollout modes off/shadow/vtr_canary/active + auto-rollback conditions — ALL WIRED BUT DEFAULT-OFF | CC merges; Clau audits vs §6/§8 |

Between checkpoints: keep working. Don't block on review latency — rebase and continue on the
next checkpoint's branch.

## 3. Hard boundaries (unchanged by the wider grant)

1. **Inert by construction.** Default flag = `off`. No live routing, no agent consumes cards,
   no prompt injection, nothing deployed. Same posture as your GSTR-1 module today.
2. **Migrations written, never run.** No dev, no prod, no local shared DB. CC executes them
   under Fazal's authorization at activation time. Numbers come from Clau.
3. **No consoles, no secrets, no env mutation, no deploys.** Egress-dependent canaries: build
   runnable, flag for CC.
4. **Effects stay gated.** Nothing in O8 may bypass or duplicate the deterministic effect
   gates. Retrieval informs reasoning; the gates still decide.
5. **Sealed set: never yours.** If sealed content ever reaches your context, stop and say so.

## 4. The existing 118-card corpus — candidate input, not a shipped asset

`archives/business-knowledge/` (118 active cards, your extraction + the 2026-07-26 quality
audit) is **the first input to WP4 ingestion, not a bypass of it.** Required treatment:

- **Rights/licensing pass FIRST.** Record `UsageRights` per source. The five `live_link_only`
  rows and any paid/restricted material need explicit rights status before their cards can
  become retrieval-eligible. The gitleaks incident (public tokens embedded in archived
  third-party HTML) is evidence the raw archive carries junk — raw text stays non-retrievable
  regardless.
- **Re-key to the card schema**: claim_key, independence clusters (the audit's consolidation
  reasoning is useful input here), source class (T1/T1v/T2/T3/T4 — your `trust_level` field is
  a starting point, not a mapping), applicability (jurisdiction, size, industry, maturity,
  channel, effective period), taint, confidence.
- **Enter as candidates.** No card is validated by having been authored well. Admission is
  measured impact against the baseline (§6) — including yours.
- Keep the quality audit's honesty conventions (never claiming a local synthesis is the source
  original). That standard carries into the registry.

## 5. Sequencing and the baseline

- Build order: A → B → C. You may build all of it before any baseline exists.
- **What you may NOT do without the baseline: claim any corpus version improves anything, or
  mark any card validated.** Admission runs after Clau's sealed set + CC's frozen baseline
  land. Build the machinery that will run it; don't pre-judge its verdict.
- Report per checkpoint: what landed, spec sections implemented, tests + results, deferrals,
  and anything that made you doubt the spec (that last one is genuinely valuable — you've
  caught two real design errors already).

## 6. Definition of done for this grant

A complete, inert, reviewable knowledge engine: contracts + registry + ingestion + retrieval +
learning + admission + rollout machinery, all default-off, migrations unrun, corpus ingested
as candidates with rights recorded, zero live consumers. Activation is a separate Fazal grant
after the baseline exists and CC+Clau review passes.
