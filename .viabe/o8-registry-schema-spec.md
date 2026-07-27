# O8 Card Registry — canonical schema, ownership boundaries, migration numbers

**Clau-issued input to VT-709 (Codex blocker resolved, 2026-07-27).** Authoritative for the
nine tables, their tenant/global ownership, RLS posture, and DSR treatment. Codex implements
SQL to THIS; deviations must be raised, not chosen.

## Allocated migration numbers (do NOT pick others)

- **182** — `182_vt709_o8_card_registry.sql` — the seven GLOBAL tables (§1).
- **183** — `183_vt709_o8_tenant_evidence.sql` — the two TENANT-SCOPED tables (§2) + their
  RLS/FORCE-RLS + DSR registration.

Split deliberately: global knowledge and tenant-scoped evidence have different privacy
postures and must be reviewable (and revertible) independently. **Both are WRITTEN, NOT RUN**
(brief §3.2) — CC executes under Fazal's authorization at activation.

## 0. The ownership rule (the thing that must not be gotten wrong)

Two classes, never mixed in one table:
- **GLOBAL** (no `tenant_id` column at all): curated knowledge — cards, sources, versions,
  evaluations, lifecycle. A global row may NEVER contain a tenant identifier, a raw tenant
  narrative, or a uniquely-identifying scenario. Enforced structurally: the column doesn't
  exist, so a leak requires putting tenant data in a text field — which §3's tests target.
- **TENANT-SCOPED** (`tenant_id UUID NOT NULL` + RLS + FORCE RLS): per-tenant evidence and
  attribution. Never readable across tenants; always DSR-purgeable.

L3 cross-business priors live in the GLOBAL class as cards with `scope='prior'` — they arrive
there only via the k-gate + contribution caps (VT-711), never by direct write.

## 1. GLOBAL tables (migration 182 — no tenant_id, no RLS needed; service-role write only)

| # | Table | Holds | Key relationships |
|---|---|---|---|
| 1 | `knowledge_sources` | canonical URL, publisher, source_class (T1/T1v/T2/T3/T4), content hash, acquisition time, **usage_rights**, retention class, taint | parent of cards via link table |
| 2 | `knowledge_cards` | **immutable card versions**: claim, claim_key, typed claim_value, distillation note, applicability (jurisdiction/size/industry/maturity/channel/effective_from/effective_until), confidence, scope (`global`\|`prior`), status (candidate/validated/disputed/superseded/expired/quarantined/research_only), version, supersedes_card_id | self-referential supersession; NEVER a tenant_id |
| 3 | `knowledge_card_sources` | provenance edges + **independence_cluster_id** (N retellings of one study = one corroboration) | M:N cards ↔ sources |
| 4 | `knowledge_corpus_versions` | releasable snapshot identity + admission verdict | parent of members |
| 5 | `knowledge_corpus_members` | which card versions are in which corpus version | M:N corpus ↔ cards |
| 6 | `knowledge_evaluations` | O11 baseline/treatment runs, A/B results, ablation outcomes, bound to corpus_version_id (+ optional card_id for ablation) | FK corpus_versions, cards |
| 7 | `knowledge_lifecycle_events` | **append-only** promotion/dispute/quarantine/supersession/rollback history; actor + reason + idempotency key | FK cards; no updates, no deletes |

Constraints: card rows immutable after insert (new state = new version row + lifecycle event);
`knowledge_lifecycle_events` append-only (revoke UPDATE/DELETE); every status transition
attributable and idempotent.

## 2. TENANT-SCOPED tables (migration 183 — `tenant_id UUID NOT NULL`, RLS + **FORCE ROW LEVEL SECURITY**)

| # | Table | Holds | Notes |
|---|---|---|---|
| 8 | `decision_evidence_links` | retrieval → decision → outcome attribution: which card versions were retrieved/selected/rejected for which run/decision, and the observed outcome | the substrate for §6 causality/ablation; PII-free by construction (ids + scores only) |
| 9 | `knowledge_incidents` | suspected + confirmed harmful-card events for a tenant: incident class, card_id, evidence refs, quarantine action, resolution | links GLOBAL card_id + tenant context; free-text fields are redacted-before-write |

## 3. DSR + privacy (the scar-tissue section — non-negotiable)

- **Both tenant tables register in `_PURGE_ORDER`** (`dsr_purge.py`) in the SAME migration that
  creates them. Order: `decision_evidence_links` then `knowledge_incidents` (children-first).
  Reason, verbatim from the existing code: on DSR the **tenants row is anonymized, NOT deleted
  — the CASCADE never fires**, so an unswept table survives the purge (the VT-366/369 lesson,
  which we have now repeated twice). A migration that creates a tenant table without a purge
  entry is INCOMPLETE, not "to be followed up".
- **PII redaction before embedding** (already implemented by Codex) applies to every card body
  and every text field that reaches an embedding call.
- **Global-purity tests (required in the checkpoint):** (a) no global table has a tenant_id
  column; (b) a seeded tenant identifier cannot be found in any global text field after an
  ingestion + prior-promotion run; (c) hard-delete canary — DSR-purge a tenant, then assert
  zero rows remain in both tenant tables (do NOT trust the FK); (d) cross-tenant read attempt
  under RLS returns zero rows, not an error-swallowed empty.
- Retention: sources/cards carry a retention class; expired cards move to `expired` status via
  lifecycle events — never hard-deleted (audit integrity), except where usage rights require
  removal, which IS a hard delete plus a lifecycle event recording it.

## 4. Naming + conventions

`snake_case`, plural table names, `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`,
`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, FKs `<singular>_id`, indexes on every FK and
on `(claim_key, status)` + `(scope, status)` for retrieval, and a partial index for
`status='validated'`. Match the style of `migrations/180_vt691_*` for header comments: what,
why, VT row, and the reversal note.

## 5. What Codex still may NOT do

Run either migration anywhere · pick further numbers (184+ are unallocated; ask) · wire any
live consumer · deviate from §0/§3 silently. If the shape here appears wrong for a real
constraint you hit in code, RAISE IT — schema disagreements are architecture decisions, not
implementation details.
