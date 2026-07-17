> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# L0 per-tenant k-anonymity admission design (VT-225)

**Status:** DRAFTED BY CC — AWAITS FAZAL REVIEW BEFORE VT-225 IMPLEMENTATION BEGINS

## 1. Problem statement

VT-126 substrate ships read-side k-anonymity: `query_l0` returns only fragments whose `observation_count >= 10`. VT-196 production-write wiring respects consent (CL-390) + PII gate but does NOT enforce admission-side k-anonymity per CL-28 intent.

CL-28 intent: "K-anonymity reverted to k=10 per concept doc Section 10." Spirit = **at least 10 distinct tenants must contribute** to a (business_archetype, signal_kind) cell before its content is exposed cross-tenant. Single-tenant fragment poisoning (one tenant writes 10 observations from itself) currently passes the read gate because `observation_count` is a row-level counter, not a distinct-tenant counter.

This doc compares two schema options + three concurrency strategies + the migration plan + canary delta. Implementation deferred pending Fazal sign-off.

## 2. Option A — `contributors UUID[]` column

Add `contributors UUID[] NOT NULL DEFAULT '{}'::uuid[]` to `l0_fragments`. On every write:

```sql
INSERT INTO l0_fragments (fragment_type, cohort_key, content, contributors)
VALUES ($1, $2, $3, ARRAY[$tenant_id]::uuid[])
ON CONFLICT (fragment_type, cohort_key) DO UPDATE
  SET observation_count = l0_fragments.observation_count + 1,
      last_observed_at = now(),
      contributors = (
        SELECT array_agg(DISTINCT t)
        FROM unnest(l0_fragments.contributors || ARRAY[EXCLUDED.contributors[1]]) AS t
      );
```

Read-side gate:

```sql
WHERE cardinality(contributors) >= 10
```

**Pros:**
- Single table; trivial JOIN-free admission query
- Backfill = `'{}'::uuid[]` for existing rows (gracefully excluded from admission until they accrue contributors)
- One INSERT per write (no second-table round-trip)

**Cons:**
- Array bloat at long tail (high-volume cohort cells may accumulate 1000+ UUIDs in one column)
- DISTINCT-on-array via UNNEST runs on every write — quadratic worst case in array length
- Concurrency: read-then-write race within the UPDATE if two transactions overlap (Postgres ON CONFLICT serializes per-row but the UPDATE itself reads the previous contributors value, leaving a window where two writes from the same tenant both think they need to append)
- Migration to extract contributors later (if scaling pressure) requires reads-and-rewrites

## 3. Option B — separate `l0_cell_contributors(fragment_id, tenant_id)` table with PK

```sql
CREATE TABLE l0_cell_contributors (
  fragment_id BIGINT NOT NULL REFERENCES l0_fragments(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  first_contributed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (fragment_id, tenant_id)
);
CREATE INDEX idx_l0_cell_contributors_fragment ON l0_cell_contributors(fragment_id);
```

Write path:

```sql
-- 1. UPSERT the fragment as today
INSERT INTO l0_fragments (...) ON CONFLICT DO UPDATE ... RETURNING id;
-- 2. Idempotent contributor INSERT
INSERT INTO l0_cell_contributors (fragment_id, tenant_id)
VALUES ($1, $2) ON CONFLICT DO NOTHING;
```

Admission read:

```sql
SELECT count(*) FROM l0_cell_contributors WHERE fragment_id = $1;
-- OR for the query path that needs it inline:
WHERE EXISTS (
  SELECT 1 FROM l0_cell_contributors c
  WHERE c.fragment_id = l0_fragments.id
  GROUP BY c.fragment_id HAVING count(*) >= 10
)
```

**Pros:**
- Normalized; idempotent at the schema layer (PK guarantees no duplicate (fragment, tenant) pairs)
- Index-friendly count
- ON CONFLICT DO NOTHING naturally handles concurrency (Postgres guarantees PK uniqueness without any explicit locking)
- ON DELETE CASCADE on `tenant_id` makes DSR-purge (CL-330 / CL-416) trivial — operator purges the tenant, contributor rows go with it
- Schema scales: 10k tenants per cell is just 10k narrow rows, not 10k UUIDs in one wide column

**Cons:**
- Two writes per L0 admission (extra round-trip on hot path; mitigated since the contributor INSERT can be a single statement chained in the same transaction)
- Admission query has a JOIN or a COUNT subquery — slightly more expensive than `cardinality(array)` but still index-served
- One more migration to manage

## 4. Concurrency semantics

Three options. The choice interacts with the schema choice above.

### (a) `SELECT ... FOR UPDATE` row-lock

Serialize per-fragment via explicit row lock during the read-then-write window. Works with Option A's array-mutation path.

**Cost:** lock contention on hot cells; throughput collapses if many tenants write to the same cell concurrently.

### (b) Postgres advisory lock on `hash(fragment_type, cohort_key)`

```sql
SELECT pg_advisory_xact_lock(hashtext($fragment_type || ':' || $cohort_key));
```

Cheaper than row-locks at large scale; auto-released at transaction end.

**Cost:** still serializes per-cell; hash collision (extremely rare on 64-bit) could cause unrelated cells to serialize.

### (c) Accept-race + idempotent insert

Don't lock. Let two concurrent writes both think they're adding a new contributor. The schema's PK or UNIQUE constraint catches the duplicate; ON CONFLICT DO NOTHING swallows it.

**Works naturally with Option B.** Option A's array-append cannot use this strategy without extra logic to dedupe post-hoc.

## 5. DSR purge interaction (CL-416, CL-330)

CL-416: lifetime-of-relationship retention; DSR-purge is the sole deletion path.

- **Option A**: removing tenant from contributors array on DSR purge requires `array_remove(contributors, $tenant_id)` per affected row — has to scan rows where the tenant ever contributed. No FK; needs application-side enumeration or a denormalized lookup.
- **Option B**: `DELETE FROM tenants WHERE id = $tenant` cascades to `l0_cell_contributors` automatically. Admission re-evaluates correctly without further intervention.

Option B is materially cleaner here.

## 6. Migration plan

### Option A migration

```sql
ALTER TABLE l0_fragments
  ADD COLUMN contributors UUID[] NOT NULL DEFAULT '{}'::uuid[];
CREATE INDEX idx_l0_fragments_contributors_gin
  ON l0_fragments USING GIN(contributors);
```

Existing rows backfilled to empty array; gracefully excluded from admission until they accrue 10 distinct contributors. No data loss.

### Option B migration

```sql
CREATE TABLE l0_cell_contributors (
  fragment_id BIGINT NOT NULL REFERENCES l0_fragments(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  first_contributed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (fragment_id, tenant_id)
);
CREATE INDEX idx_l0_cell_contributors_fragment ON l0_cell_contributors(fragment_id);
```

Existing rows: no backfill possible (we don't have historic tenant attribution per fragment). Gracefully excluded from admission until they accrue 10 distinct contributors. Same UX as Option A.

## 7. Canary delta — `vt196_l0_prod_writes.py` extension

Add A4-A6:

- **A4:** 10 writes from 10 distinct tenants to the same cell → admission passes; `query_l0` returns the fragment
- **A5:** 10 writes from 1 tenant to the same cell → admission rejects; `query_l0` returns empty (this is the load-bearing test against poisoning)
- **A6:** 11th tenant writes → fragment row contributor count becomes 11; still admitted; no observation_count regression

If Option B chosen, also:
- **A7:** DELETE tenant; `l0_cell_contributors` rows for that tenant gone; admission re-evaluates correctly (contributor count drops by 1)

Real Postgres; no mocks. Seed 10 tenants via existing helper.

## 8. Recommendation

**Option B + concurrency strategy (c) — accept-race + idempotent insert.**

Reasoning:
1. **Normalized schema scales** without per-row array bloat. 1000 tenants per cell = 1000 narrow rows, not 1000 UUIDs in one column.
2. **DSR purge is trivial** via FK CASCADE — the privacy substrate posture (CL-330, CL-416) is materially cleaner.
3. **No application-layer locking** needed. PK uniqueness + ON CONFLICT DO NOTHING handles all concurrent-write semantics correctly without explicit locks or advisory primitives.
4. **Auditability**: `first_contributed_at` per row gives a free contributor-history timeline (useful for future trust scoring or pattern decay).

Trade-off accepted: 1 extra write per L0 admission. At Phase-1 scale (few hundred admissions/day) this is invisible. At Phase-2 scale, both writes can fold into a single CTE statement.

If Cowork prefers Option A (simpler at the source-code layer), the fallback should be **A + strategy (b) advisory lock**. Strategy (a) row-lock + Option A's UNNEST is the worst combination at long tail and should be avoided.

## 9. Implementation plan (for the follow-up VT row)

When Fazal approves, the implementation row should:

1. Ship the migration (Option B per recommendation)
2. Wrap the existing `write_l0_fragment_workflow` with the contributor-INSERT + admission-count return
3. Update `query_l0`'s read-side gate to use contributor count (or keep observation_count + contributor as a redundant double-check during the transition window)
4. Ship the A4-A7 canary extensions
5. Document the migration story in a runbook entry

Estimated diff: ~250 LOC source + 1 migration + ~150 LOC canary delta.

## 10. References

- CL-28 (k-anonymity k=10)
- CL-281 (operator-claim JWT)
- CL-324 (memory tiering: L0 in-house)
- CL-330 (owner_inputs structured intent)
- CL-390 (consent gate cluster)
- CL-416 (lifetime retention; DSR-purge sole deletion path)
- VT-126 (L0 substrate)
- VT-196 (production write wiring — current state without admission gate)
- ADR-0009 (memory tiering decision record)

---

*This document is a design proposal awaiting Fazal review. Implementation is gated on that review. CC will not begin coding admission-gate substrate until Cowork dispatches a follow-up implementation row referencing the approved option.*
