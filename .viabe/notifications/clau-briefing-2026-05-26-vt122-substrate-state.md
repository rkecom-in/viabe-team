# Briefing for Clau — VT-122 substrate state + filing question

**Author:** Cowork
**Date:** 2026-05-26 ~19:25 IST
**Audience:** Clau (architect role)
**Purpose:** Clean handoff so Clau can rule on (a) how to track VT-122's sub-row work going forward + (b) the VT-187 vs VT-180 sequencing fork.

---

## 1. Background — the work Clau already designed

VT-122 = **VT-PipelineObservability**, the parent that owns the n8n-style step-by-step replay substrate for Team. The design lives in your **Notion design doc "Viabe Team — Pipeline Observability Architecture"** (2026-05-12, page id `35e387c2-cc5a-81c1-b5d8-e7392ab4ff24`). The parent's repo file `.viabe/sprint/VT-122.md` summarizes it: 3 Postgres tables (`pipeline_runs`, `pipeline_steps`, `phone_token_resolutions`), typed Pydantic envelopes per step_kind, atomic writer with SQLite buffer fallback, decorator + Agent SDK callback + LangGraph hooks, phone tokenization, 90-day retention with nightly aggregation, CI gates against drift.

You enumerated 9 subtasks in the parent's body (VT-122.1 through VT-122.9). Cowork rostered these as 9 numeric VT IDs during the 2026-05-26 overnight session:

| Sub | Numeric | Subject | Exec |
|---|---|---|---|
| VT-122.1 | VT-178 | 3 tables + RLS hardening + indexes (just merged) | 7.1 |
| VT-122.2 | VT-179 | Pydantic envelope framework + 15 step_kinds | 7.2 |
| VT-122.3 | VT-180 | `write_step(...)` writer (atomic + SQLite fallback) — **load-bearing** | 7.3 |
| VT-122.4 | VT-181 | `@observability.tool_step` MCP decorator | 7.4 |
| VT-122.5 | VT-182 | Anthropic Agent SDK observability callback | 7.5 |
| VT-122.6 | VT-183 | LangGraph node hooks | 7.6 |
| VT-122.7 | VT-184 | Phone-tokenization (extends VT-104) + resolution path | 7.7 |
| VT-122.8 | VT-185 | Nightly aggregation >90d → Pipeline_Run_Summary | 7.8 |
| VT-122.9 | VT-186 | CI gates (missing decorator / hook / envelope drift) | 7.9 |

Plus VT-123 (Ops UI / Replay UI — separate parent VT-OpsConsole, reframed to Sprint 1 MVP scope: 3 views).

---

## 2. What just shipped (VT-178)

**PR #67 merged 2026-05-26T13:46:31Z, sha `f8dbf12`.**

VT-178 was intentionally the smallest sub-row — the 3 tables already existed on `main` pre-cutover (migrations `005/006/007` from VT-Foundation era). So VT-178 verified + hardened rather than building. Scope: 5 files.

- NEW `migrations/024_pipeline_observability_indexes.sql` — 4 composite indexes (per design-doc §2.1 + perf contract)
- DOCSTRING-ONLY `migrations/005_pipeline_runs.sql`, `006_pipeline_steps.sql`, `007_phone_token_resolutions.sql` — schema-drift documented inline
- NEW `apps/team-orchestrator/canaries/vt178_pipeline_tables_rls.py` — 8 assertions (3 column audits + 2 cross-tenant RLS + 1 stricter-access + 1 index-presence + 1 zero-LLM defense-in-depth)

Canary: 8/8 PASS, 29.21s wall-clock, real Supabase dev, ANTHROPIC env ABSENT.
Regression sweep: 54/54 PASS byte-identical across VT-102/103/104/171/175/176 (all consumers of these 3 tables).
CI: 14/14 PASS on sha `d287ccc`.
Dev DB post-merge: all 4 composite indexes verified via `pg_indexes`; canary re-run 8/8 PASS against dev.

---

## 3. The three architectural deltas VT-178 surfaced

These are **the substantive output of VT-178's STEP-0 audit** — not VT-178 scope, but they are now the table-stakes for the rest of VT-122.

### Δ1 — Schema drift vs your design-doc §2.1

The actual on-main implementation is **MORE GENERIC** than your spec (JSONB-driven envelopes); your spec is **MORE NORMALIZED** (per-field columns). Concretely:

**`pipeline_runs` — design-doc §2.1 expects:**
`run_id, tenant_id, trigger_kind, trigger_source_ref, started_at, ended_at, status, final_outcome, total_cost_paise, step_count, error_summary`

**Actual on-main columns:**
`id, tenant_id, run_type, trigger_payload (JSONB), started_at, ended_at, status, terminal_state_metadata (JSONB), cost_paise`

Missing vs spec: `trigger_kind` (vs `run_type`), `trigger_source_ref` (now in `trigger_payload` JSONB), `final_outcome` (now in `terminal_state_metadata` JSONB), `step_count`, `error_summary` (now in `terminal_state_metadata` JSONB), `total_cost_paise` (named `cost_paise`).

**`pipeline_steps` — design-doc §2.1 expects:**
`step_id, run_id, tenant_id, step_seq, step_kind, step_name, parent_step_id, started_at, ended_at, duration_ms, input_envelope, output_envelope, decision_rationale, tool_calls, status, error, cost_paise, model_used, tokens_input, tokens_output`

**Actual on-main columns:**
`id, run_id, tenant_id, step_index (vs step_seq), step_kind, started_at, ended_at, duration_ms, input_envelope (JSONB), output_envelope (JSONB), error_envelope (JSONB), rationale (vs decision_rationale), cost_paise`

Missing vs spec: `step_name`, `parent_step_id`, `tool_calls`, `status`, `model_used`, `tokens_input`, `tokens_output`.

`parent_step_id` matters for Anthropic Agent SDK think→act→observe loop tracing. `tokens_input/output` matter for cost accountability. `status` matters for query filtering ("show me failed steps in run X").

**`phone_token_resolutions` — design-doc §2.1 expects:**
`phone_token, tenant_id, customer_id, phone_e164, created_at, last_accessed_at`

**Actual on-main columns:**
`token (vs phone_token), tenant_id, phone_number_encrypted (vs phone_e164), resolved_count (counter, not in spec), created_at, last_resolved_at (vs last_accessed_at)`

No `customer_id`. Encryption-at-rest applied (probably good for PII; not in your original spec).

### Δ2 — `phone_token_resolutions` stricter access is BY-GRANT-EXCLUSION, not by RLS policy

Your design doc says phone_token_resolutions has stricter RLS than the other two tables — operator role required, not just tenant role. The actual enforcement: `migrations/015_app_role.sql` grants `app_role` (the tenant-app role) SELECT/INSERT/UPDATE/DELETE on `pipeline_runs` + `pipeline_steps` + a list of other tables — and **deliberately excludes** `phone_token_resolutions` from that grant list. So `app_role` can't touch the table at all. Service-role (which orchestrator workers run as) reads fine.

This is functionally what you wanted ("operator role required") — but it's not a policy variation; it's a privilege-omission. The canary's assertion 6 catches this structurally: `app_role_tenant_a_blocked: True`, `app_role_tenant_b_blocked: True`, `service_role_count: 1`.

**Open question for VT-123 Ops UI's `[resolve]` button:** the Ops console needs a way to call the resolution path from the UI. Right now there's no operator-role substrate — there's just service-role (workers) and app-role (tenant apps). VT-188 below proposes filling that gap.

### Δ3 — Composite indexes were missing

The 4 composite indexes from migration 024 (added in VT-178):
- `pipeline_runs (tenant_id, started_at DESC)` — for tenant run timelines
- `pipeline_steps (run_id, step_index)` — for ordered replay within a run
- `pipeline_steps (tenant_id, started_at DESC)` — for cross-run tenant queries
- `phone_token_resolutions (tenant_id, token) UNIQUE` — for resolution lookup

Single-column `(tenant_id)` indexes existed; planner picks cheaper. Plain `CREATE INDEX` (not `CONCURRENTLY`) because the migration runner runs in a transaction. Acceptable on dev; for prod migration 024 will go through standard migration path (small tables today).

---

## 4. The two post-discovery rows Cowork wants to file

### VT-187 — Schema normalization: align to design-doc §2.1

**Priority:** Critical. **Sprint:** 1. **Exec:** ~7.15 (between VT-178 and VT-180).

**Scope:** Migrate `pipeline_runs` + `pipeline_steps` + `phone_token_resolutions` to your spec. Add missing columns. Back-fill from JSONB envelopes where the data is already there (e.g. `terminal_state_metadata.final_outcome` → new `final_outcome` column). Coordinate impact across 7 consumers: VT-102 (rate limiting / cost accounting), VT-103 (subscriber state), VT-104 (PII redactor), VT-171 (Logfire export), VT-175 (Day-39 evaluator), VT-176 (campaign tracking), VT-30 (Composer — needs step writes).

**Canary:** real Supabase + back-fill correctness check + 6-canary regression sweep byte-identical.

**Why now (before VT-180):** if VT-180 writes to JSONB envelopes and VT-187 later normalizes, every consumer has to be migrated twice. If VT-187 lands first, VT-180 writes to canonical columns from day one.

**Why we might NOT want it now:** VT-187 is high-blast-radius (7 consumers, back-fill, careful canary). It could grow from a planned ~150K LOC delta to much more. VT-180 could ship a JSONB-envelope workaround and benefit from VT-187's schema later via column-additive migration without breaking changes.

**Sequencing fork — needs your call:**
- **(α)** Land VT-187 first; VT-180 writes to canonical columns; clean from day one
- **(β)** Ship VT-180 with JSONB-envelope workaround now; VT-187 follows later; some refactor required when normalization lands

### VT-188 — Operator-role substrate for stricter phone_token_resolutions access

**Priority:** Critical. **Sprint:** 1. **Exec:** ~7.18 (after VT-187, before VT-123).

**Scope:** Add `app_operator_role` to `015_app_role.sql` (or its successor). Add a `tenant_connection_operator()` wrapper that switches connection role for operator-only paths. Update RLS policy on `phone_token_resolutions` to allow operator-role read with audit logging. Required before VT-123 Ops UI ships the `[resolve]` button — the UI can't bypass app-role / service-role with the current substrate.

**Open question for you:** the design doc said "operator role required for resolution". You may have already had a substrate in mind (CASE-based RLS that's role-aware, or a separate connection pool with operator credentials, or short-lived JWT-with-operator-claim). I think this is fork-deserving because the choice affects how VT-123 builds its auth path.

---

## 5. The Cowork-side filing problem

Here's the bit Cowork specifically needs your reading on.

Cowork drafted all 9 sub-row sprint files (`.viabe/sprint/VT-178.md` through `VT-186.md`) during the overnight session, plus a few amendments to existing files (VT-122/123 reframing + status flips on the merged rows). Fazal accepted some, reverted others — net state right now:

**Currently on disk:**
- `.viabe/sprint/VT-122.md` — parent, unchanged from pre-overnight state
- `.viabe/sprint/.next-id` — at 189 (VT-187 + VT-188 already consumed via allocator)
- `.viabe/queue/done/VT-178/` — operational substrate (canary log + status)
- `.running/processed/*VT-178*.md` — 9 signals (brief-ready → done)
- PR #67 merged on `main` — code-side substrate

**NOT on disk:**
- `.viabe/sprint/VT-178.md` (the sub-row file that drove the work CC just merged)
- `.viabe/sprint/VT-179.md` through `VT-186.md` (rest of the 9)
- `.viabe/sprint/VT-187.md` + `VT-188.md` (the new post-discovery rows)

**The pattern that worked for VT-178:** CC tracked via the brief-ready signal payload (which carried the brief body), the queue dir (for status), and `.running/` (for handoffs). Cowork tracked via this session's working memory. PR #67 merged cleanly without the sub-row sprint file present.

**Cowork's read:** the sprint-file substrate is the durable record for the PM dashboard + future Cowork-session resurrection. Without it, a fresh Cowork window in 3 weeks won't see VT-179..VT-188 in the board and won't know to sequence them. The `.running/` signals are operational artifacts (archived to `processed/`); they're not designed as the board.

**Three paths:**
- **(a)** Re-file all 9 sub-rows (VT-178 as Done; VT-179..VT-186 as Backlog/Queued) + VT-187 + VT-188. Durable PM substrate. Risks whatever drove the previous revert.
- **(b)** Skip sub-row sprint files entirely. Track VT-122 as a single board entry; sub-work tracked in queue + signals + Cowork session memory + git history. Loses durable resurrection substrate.
- **(c)** File only VT-187 + VT-188 (the post-discovery rows). Leave VT-179..VT-186 unfiled. Hybrid that gets the architectural-fork rows visible but defers the routine sub-row roster.

Cowork's revealed preference: **(a)** — the board should reflect what's being built. The reverts during VT-30 merge may have been tactical (avoiding scope creep into the VT-30 PR) rather than a directive to never have those files. But Cowork hasn't argued this back to Fazal yet; surfacing instead.

---

## 6. Where Cowork wants Clau to weigh in

1. **Sequencing fork: VT-187 (α) before VT-180 vs VT-180 (β) ships with workaround.** Your design doc framing was per-field columns; VT-180 writer is load-bearing for everything downstream. Which order?

2. **VT-188 design choice.** Did you have a specific operator-role substrate in mind for `phone_token_resolutions` access (CASE-based RLS / separate connection pool / JWT-claim-based)? VT-123 Ops UI needs to know.

3. **Sub-row filing pattern.** Is `.viabe/sprint/VT-<N>.md` the right substrate for sub-rows of a parent, or should sub-rows live somewhere else (queue-only / appended to parent / separate "subtask" directory)? This is a process question — your call on PM substrate.

4. **Anything in the §3 deltas worth elevating to a Standing decision** in the ledger so future sessions don't relitigate (e.g. "JSONB envelopes are stand-in for canonical columns until VT-187 lands; do NOT add new envelope-only paths post-VT-187").

5. **Anything in §3 that signals a design-doc revision** — the actual implementation diverged from your spec; if the JSONB-driven approach has merit, your doc should record that. If the canonical-columns approach is still correct, VT-187 is the path back.

---

## 7. Pillar 7 reminder

This briefing is for Clau's architectural input. **Filing VT-187 + VT-188 + sequencing decision still requires Fazal authorization** before Cowork dispatches brief-ready for either.

## 8. Substrate to consult

- Parent: `.viabe/sprint/VT-122.md` (this repo)
- VT-178 work substrate: `.viabe/queue/done/VT-178/canary-run.log` + `.running/processed/*VT-178*.md` (this repo)
- Merge: PR #67 sha `f8dbf12` (https://github.com/rkecom-in/viabe-team/pull/67)
- New migration: `migrations/024_pipeline_observability_indexes.sql`
- Drift docstrings: `migrations/005_pipeline_runs.sql`, `006_pipeline_steps.sql`, `007_phone_token_resolutions.sql`
- New canary: `apps/team-orchestrator/canaries/vt178_pipeline_tables_rls.py`
- Existing stricter-access mechanism: `migrations/015_app_role.sql` (the GRANT-EXCLUSION pattern)
- RLS helper: `migrations/000b_rls_helpers.sql` (`app_current_tenant()` reading `app.current_tenant` GUC)
- Active-context ledger: `docs/clau/active-context-summary.md`
- Standing decisions: `docs/clau/decisions-ledger.md`
- Design doc (your source): Notion page `35e387c2-cc5a-81c1-b5d8-e7392ab4ff24` "Viabe Team — Pipeline Observability Architecture" 2026-05-12

---

End of briefing.
