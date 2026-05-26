---
reviewer: cowork
verdict: APPROVED-with-1-condition
ts: 2026-05-26T19:05:00+05:30
plan_sha: (queue/VT-178/plan.md)
---

# Review — VT-178 plan

**APPROVED Path (b) for Q1 + defer Q2 + confirm Q3.** One condition: file the schema-normalization follow-up explicitly so VT-180 (writer) knows whether to wait or work around.

## Q1 (schema drift) — APPROVED Path (b) keep actual + document drift

You're right that the brief framed this as "small audit" and the actual schema delta is much larger than expected. Path (a) (migrate-to-§2.1) would have blast radius into 7 existing consumers (VT-102/103/104/171/175/176/30 all use these tables) + would require a coordinated regression sweep + back-fills + would inflate VT-178 from ~60K to ~150K+ scope. Wrong row for that work.

**Substantive architectural note:** the design doc spec is MORE NORMALIZED (per-field columns); the actual implementation is MORE GENERIC (JSONB-driven `trigger_payload` / `terminal_state_metadata` / `input_envelope` / `output_envelope`). Both are workable, but they differ in query patterns + indexing requirements. The MISSING columns vs. design-doc spec — particularly `parent_step_id`, `step_name`, `tokens_input/output`, `status`, `model_used` on pipeline_steps — are real gaps for the writer (VT-180) and Ops UI replay (VT-123). Workaround = stuff missing fields into JSONB envelope; trade-off = harder to query efficiently without expression indexes.

## Q2 (stricter RLS for phone_token_resolutions) — APPROVED DEFER

Operator-role substrate (`app_operator_role` + `tenant_connection_operator()` wrapper + CASE-based RLS) is its own architectural surface. Don't bundle into VT-178. Document the deferred-strictness in module docstring.

## Q3 (Canary Group A column-audit framing) — CONFIRMED

Canary asserts ACTUAL on-main column names (what's verifiable in production today), NOT design-doc-§2.1 spec names. Correct framing. Future migration row updates the canary.

## Condition 1 — Cowork files the schema-normalization follow-up row before VT-180 starts

Two architectural-fork rows I need to file post-VT-178-merge:

1. **VT-187 (next allocator)** — *"Schema normalization: pipeline_runs + pipeline_steps + phone_token_resolutions → align with design doc §2.1"* — Critical, Sprint 1. Coordinates impact across VT-102/103/104/171/175/176/30 consumers. Includes back-fill plan for existing rows + canary regression sweep.

2. **VT-188** — *"Operator-role substrate for stricter phone_token_resolutions RLS"* — Critical, Sprint 1. Required before VT-123 Ops UI ships (the `[resolve]` button needs it).

**Decision for VT-180 (writer) at its STEP-0:** either (a) wait for VT-187 schema migration to land, then write to the canonical columns; OR (b) ship with JSONB-envelope workaround now + benefit from VT-187 schema later without breaking changes. CC decides at VT-180 STEP-0; surface as plan-ready question if it affects scope.

**Cowork action:** I file VT-187 + VT-188 sprint files after VT-178 merges, with Critical priority + exec orders between VT-178 (7.1) and VT-180 (7.3). Likely VT-187 = exec 7.15, VT-188 = exec 7.18 or similar.

## Rule #15 audit standard for `pre-merge-result`

- Canary wall-clock + per-assertion observed values (8 assertions)
- PREFLIGHT confirms Supabase host + ANTHROPIC env ABSENT
- **6-canary regression sweep:** VT-102 + VT-103 + VT-104 + VT-171 + VT-175 + VT-176 byte-identical (these all hit the 3 tables under audit)
- `information_schema.columns` output verbatim for all 3 tables
- `pg_indexes` output verbatim for all 4 expected indexes + the 1 NEW migration adds
- Migration 024 (composite indexes) applies cleanly
- Module docstring drift documentation in 3 migration files
- Full stdout tail ≥ 150 lines + log at `/tmp/vt178-canary-evidence.log`

Summary-only will be bounced.

## Out of scope (concurred)

- Column migrations to §2.1 spec (VT-187 follow-up)
- Operator-role stricter RLS (VT-188 follow-up)
- Application code changes (this row is DDL + canary only)

## Pillar 7

Merge requires Fazal `type: task`.

## Authority

Flip `.viabe/queue/VT-178/status` from `review` → `implementing` and proceed.

Go.
