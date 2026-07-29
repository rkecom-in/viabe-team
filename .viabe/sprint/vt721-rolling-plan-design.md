# VT-721 — rolling 7-day plan, revised daily: design note

Status: DRAFT (CC, 2026-07-31). Authorization: CL-2026-07-29-manager-is-coo (c) + Clau row.
Clau: audit-after; objections early.

## 1. Position in the estate (reuse-first)
§7A today: `business_plan/generator.py` (monthly re-ground → roadmap items, seq-ordered) +
`daily_initiative.py` (once daily: pick next accepted roadmap item → mint manager_task).
VT-721 inserts the missing MIDDLE horizon: a durable 7-day plan OBJECT between the monthly
roadmap and the daily pick — and turns the daily fire into a REVISION of that object, not a
stateless selection.

## 2. The plan object (new table, one migration — number from Clau)
`tenant_week_plans` (tenant-scoped, RLS + FORCE RLS + `_PURGE_ORDER` same migration):
- `id`, `tenant_id`, `plan_date` (the day this revision was made), `horizon_start/end` (7d),
- `actions` jsonb — ordered list, each: `{key, objective, directive, inputs, assigned_to
  (specialist|tool), expected_outcome, source (roadmap_item|reactive|carryover), status
  (planned|in_flight|done|dropped)}` — §0.1d: directive+input+objective ride ON the action,
- `revision_notes` jsonb — the WHY ledger for THIS revision: `[{action_key, change
  (keep|drop|resequence|add|amend), reason}]`,
- `prev_plan_id` self-ref (the revision chain), `created_at`.
Append-only chain (a revision = new row), mirroring manager_asserted_facts.

## 3. The daily revision pass (extends the existing daily fire — no new scheduler)
In `daily_initiative`'s per-tenant fire, BEFORE the pick:
1. Read yesterday's plan row + outcomes since: `manager_tasks` terminal states + audit trail
   (VT-514 spine) + campaign/task outcome rows — deterministic collection.
2. ONE LLM call (the Manager brain, house seam + ledger context): propose the revised 7-day
   action list + per-change reasons, grounded ONLY in the collected outcomes + active roadmap
   items + the asserted-facts ledger (a plan told to the owner is an assertion — flips must be
   owned, VT-719 substrate).
3. Deterministic post-gate: actions must reference real roadmap items/known action classes;
   money/send actions carry `requires_approval: true` ALWAYS (plan ≠ effect, §0.1.1 — the
   planner cannot pre-authorize anything); cap list length (7d × small-biz reality ≈ ≤10).
4. Write the new revision row; the daily pick then selects from the PLAN's next planned action
   (falling back to today's roadmap-seq rule when no plan exists — flag-gated, byte-identical
   off).
Flag: `TEAM_WEEK_PLAN` off|shadow|active (shadow = write revisions, don't alter the pick).

## 4. Surfaces
- Owner ask ("what's the plan this week?"): brain answers from the plan row via a read tool
  (`read_week_plan`) — no new send path (S2 choke governs the voice).
- VTR console: `/team/ops/tenants/<id>/week-plan` — revision chain + why-notes (activity-flow
  visual language).
- Assertion: when the plan is TOLD to the owner, record `fact_key=week_plan_headline`
  (registry addition) so contradictions are owned.

## 5. Proof
Unit: revision-gate truth table (approval invariants, cap, grounding refs) + store round-trip.
Realdb: chain + RLS + purge. Dev: seeded tenant, 3 consecutive daily fires → 3-row chain with
coherent why-notes; owner ask surfaces the plan. Full-pack ×3 (brain-touching) with pushes held.
Prod: flag stays off — Fazal's flip.

## 6. Build stages
S1 migration + store + revision gate (no LLM). S2 revision pass behind flag=shadow.
S3 pick integration + owner/VTR surfaces. S4 ×3 + graduation ask.
