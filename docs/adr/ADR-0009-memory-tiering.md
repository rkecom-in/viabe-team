# ADR-0009: Memory tiering — L0 in-house, L1 deferred (Mem0 evaluated, not adopted)

**Status:** Accepted

## Context

Multi-agent system needs durable memory across:

- **L0 — episodic fragments** — per-tenant or per-cohort observations from past runs ("this archetype + this signal = this outcome 80% of the time"). Cheap to derive; small footprint; sub-second read latency for hot-path; k-anonymity gate at admission per CL-28.
- **L1 — semantic / KG** — structured knowledge about tenants (relationships between entities; time-aware facts; cross-tenant inferences). Large footprint; vector + relational; query at planning time.

Hosted memory solutions (Mem0, Zep, MemGPT) exist; each handles one or both tiers via SaaS APIs.

## Considered Options

- **A.** Mem0 for both L0 + L1 — fast time-to-first-feature; vendor risk + cost-at-scale + cross-tenant boundary concerns + DPDP residency questions
- **B.** L0 in-house + L1 deferred to Mem0 later (chosen) — owns the hot-path; defers the harder KG question
- **C.** Pure in-house for both — slower L1 ship; full control

## Decision

**B.** L0 lives in-house: VT-126 substrate (cohort-keyed fragments in Postgres + JSONB payloads + k-anonymity admission gate). L1 KG is deferred — concept doc pending Fazal review (`docs/clau/l1-tenant-context-design.md`, not yet authored). L1 substrate may end up being:

- **B1.** In-house pgvector + relational (no AGE — Apache AGE unsupported on Supabase per memory note)
- **B2.** Mem0 SaaS — evaluated once L0 production-write wiring (VT-196) confirms read-path latency budget

The L1 decision is intentionally NOT locked in this ADR; will get its own ADR-00NN once VT-195 / Fazal-reviewed concept doc lands.

## Consequences

- (+) L0 hot-path under our control; query latency predictable; cost zero beyond Postgres bytes
- (+) k-anonymity gate (CL-28) enforced at admission — no leaking individual-tenant observations into cross-tenant fragments
- (+) DPDP residency story for memory is unified with the substrate decision (ADR-0003)
- (+) Mem0 evaluation deferred until we have real L0 production patterns to inform whether L1 needs SaaS
- (−) When L1 lands, it will need a separate substrate decision (vendor vs in-house)
- (−) L0 production-write wiring (VT-196) is non-trivial — must respect consent (CL-390) + k-anonymity admission
- (−) Concept doc for L1 (`docs/clau/l1-tenant-context-design.md`) owes Fazal — blocking item

## References

- CL-324 (memory tiering decision, L0 in-house)
- CL-28 (k-anonymity admission gate)
- CL-390 (consent gate on memory writes)
- VT-126 (L0 substrate)
- VT-196 (L0 production-write wiring)
- VT-195 (L1 tenant context substrate — gated on concept doc)
- Memory: L1 KG drops Apache AGE (pgvector + relational only)
