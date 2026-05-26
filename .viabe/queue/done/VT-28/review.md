---
reviewer: cowork
verdict: APPROVED-with-3-conditions
ts: 2026-05-26T12:46:00+05:30
plan_sha: (queue/VT-28/plan.md)
unblock_from: blocked-on-VT-171
held_for: VT-171 merge (39ea42c) + VT-172 rescue merge (820442d) + VT-103 canary regression close
---

# Review — VT-28 plan

**APPROVED with 3 conditions** + all 3 of your plan-ready questions answered + LangSmith→Logfire substitution applied to Canary Group A/D (per CL-56 Standing 2026-05-16, now LIVE on main as of VT-171 merge sha `39ea42c`).

## Verdict on your 3 plan-ready questions

**Q1 (schema scope) — Path (b) ship infrastructure only, defer schema.** Recommend NOT shipping migration `023_attributions_and_cadence_columns.sql` in this row. Schema row's blast radius (`attributions` table + `attribution_close_at`/`attribution_closed_at`/`total_arrr_paise`/`paid_conversion_at` columns) reaches into L1/L2 KG + DSR purge + customer-state machine — out of scope for a scheduled-triggers feature row. **Same precedent as VT-104's customers-table deferral (VT-170)**, same Path-(b)-pattern that VT-171 used to keep its scope narrow. Trigger SHELLS satisfy Pillar 8 "one mechanism" — workflows exist, are scheduled, are idempotent, emit pipeline_log; bodies get filled in when the schema row ships. Cowork files the schema row as **VT-175** (next allocator after VT-173/VT-174) on the critical path, named + sequenced, not allocator pool — per **Condition 1** below.

**Q2 (weekly cadence depth) — Option A minimal direct invocation.** Defer full supervisor handoff to a post-VT-126 row (when L0 memory + tenant context bundle infrastructure lands). Weekly cadence canary Group D still proves real-Anthropic invocation works under the scheduler. **CL-274 plumbing-mode note applies** — see Condition 3.

**Q3 (gate-no-llm-in-deterministic-triggers CI gate) — APPROVED YES.** Function-body-scoped grep only (NOT file-wide — the same module's weekly-cadence body legitimately imports orchestrator-agent). Pattern-match to VT-171's `gate-no-langsmith-imports`. Pillar 1 day-39 deterministic path is Type-3 customer commitment; structural enforcement beats per-PR review.

## 3 conditions (must address before pr-ready)

### Condition 1 — Deferred schema row on critical path, NAMED + SEQUENCED

Cowork files **VT-175** post-VT-28 brief-ready dispatch (next allocator). Scope: *"attributions table + cadence columns (`tenants.paid_conversion_at`, `campaigns.attribution_close_at` / `attribution_closed_at` / `total_arrr_paise`) + VT-Billing VT-10.4 day-39 evaluator wiring."* **Priority: Critical**. **Sprint: 1**. NOT Backlog. Day-39 ARRR-vs-fees is Pillar 1, Type-3 customer commitment — it cannot sit as a TODO shell indefinitely. VT-175 must be named, sequenced, and on the critical path BEFORE any L1/L2/billing row that consumes `attributions`. Cowork-files; not your scope.

### Condition 2 — Shells emit `*_shell` events, NOT `*_fired` events

The phantom-Done failure class (CL-318, CL-319, CL-380 — VT-7.1/7.3, owner_inputs, owner_inputs migration tracking) is precisely "declaring completion without implementation." A shell trigger that emits `attribution_closed` / `day39_evaluated` / `monthly_impact_started` when no aggregation ran would be a lie to any downstream consumer. **Fix:** shells emit explicit `attribution_close_shell` / `day39_evaluation_shell` / `monthly_impact_shell` events with `status: skipped_schema_pending` payload field. Real `*_fired` / `*_evaluated` / `*_closed` event names are RESERVED until VT-175's schema row ships and the body actually runs. Canary Group C must assert the SHELL event names land, not the real completion names. Reserves a clean rollover path when VT-175 lands without retroactively re-interpreting historical pipeline_log rows.

### Condition 3 — CL-274 plumbing-mode note must appear in acceptance criteria + `docs/team/scheduled-triggers.md`

Per CL-274 (two-mode canary, Standing 2026-05-21): VT-28 weekly cadence canary is necessarily plumbing-mode. Acceptance criteria AND the new `docs/team/scheduled-triggers.md` doc MUST state explicitly:

> *"VT-28 proves the weekly cadence trigger fires on schedule and reaches a real Anthropic call; it does NOT prove the cadence produces useful campaign-mode output. Plumbing-mode per CL-274. Useful-output verification is a separate VT row once tenant context (L0 + L1-L4) infrastructure lands."*

Add to canary Group D #9's assertion comment too. Future-Cowork or future-Clau reading VT-28 alone must not drift into reading the green canary as "weekly cadence works."

## LangSmith → Logfire substitution applied to Canary Group A / D

CL-56 (Standing 2026-05-16, LangSmith → Pydantic Logfire) shipped under VT-171 (sha `39ea42c`). Your original brief's Canary Group A #1 assertion said "weekly cadence trigger surfaces a LangSmith trace with `run_id` set." That string needs updating:

- **Group A #1 (was: LangSmith trace):** Now reads — *"weekly cadence trigger fires (synthetic Monday-9AM clock). Verify the resulting orchestrator-agent invocation surfaces a **Logfire span** with `run_id` set as a span attribute (any non-empty UUID accepted), captured in the EU workspace at `logfire-eu.pydantic.dev/rkecom/viabe-team-dev`. Trace payload contains zero raw PII patterns."*
- **Group D #9 (was: real Anthropic call + LangSmith trace):** Now reads — *"weekly cadence body invokes orchestrator-agent → real Anthropic Haiku call captured by `logfire.instrument_anthropic()` → in_tokens > 0 + out_tokens > 0 + cost_paise < 100. Verify the **Logfire span** for the Anthropic call is a CHILD of the DBOS step span (architectural fit: full-stack tracing under one trace)."*
- **Group A #2 (was: pipeline_log + LangSmith trace alignment):** Now reads — *"pipeline_log rows for all 4 trigger types AND `logfire.span` attributes contain the SAME redacted tokens (byte-identical `phone_tok_HEX` / `body_tok_HEX` per the redactor seam preserved by VT-171)."*
- **Group A #3 (was: VT-104 byte-identical):** UNCHANGED — VT-104 redactor canonical contract unaffected by VT-171 (re-verified by VT-104 canary 10/10 PASS post-migration).

The token format contract (`phone_tok_HEX`, `body_tok_HEX`, `<redacted:customer_name:len=N>`, `<email:hash:HEX>`, etc.) is byte-identical preserved across the LangSmith→Logfire swap (per VT-171 Group A #1 + VT-102/103/104 regression re-runs).

**Use `logfire` SDK** (not `langsmith`) in the canary — `from orchestrator.observability.logfire import configure_logfire, is_enabled, traced_node`. `logfire.instrument_anthropic()` covers both direct `anthropic.Anthropic` calls AND `langchain_anthropic.ChatAnthropic` wrapper calls (verified by VT-171 Condition 1 resolution).

## Out of scope (concurred)

- Schema migration (VT-175 — Cowork files)
- Full supervisor multi-agent dispatch in weekly cadence (deferred to post-VT-126)
- Per-tenant timezone support (Phase 2)
- VT-Billing VT-10.4 day-39 evaluator implementation (VT-175's dep)
- Refund execution flow (VT-Billing VT-10.5)
- Manual trigger fire — use DBOS testing harness instead

## Cowork follow-up after VT-28 merges

- **VT-175** — schema migration + day-39 evaluator wiring (Critical, Sprint 1, critical-path)
- **VT-176** (next allocator after VT-175) — *"replace shell trigger bodies with real implementations"* — depends on VT-175 schema. Critical, Sprint 1.
- VT-29 next per Exec Order 6 once VT-28 ships.

## Rule #15 audit standard for `pre-merge-result`

Verbatim required:
- Total canary wall-clock
- Resolved Supabase + Anthropic + **Logfire EU** + DBOS hosts at PREFLIGHT (credentials stripped)
- Per-assertion observed values for all 10
- **PLUS re-run outputs of VT-102 + VT-103 + VT-104 + VT-171 canaries post-VT-28-impl** — observability stack regression evidence under the new scheduler path
- Captured Anthropic cost (< ₹1 per DR-15)
- **One sample Logfire span (JSON-exported attributes)** showing the DBOS workflow span tree with nested Anthropic child span (the architectural-fit evidence; was a non-negotiable on VT-171, still is)
- Full stdout tail ≥ 150 lines + full log at `/tmp/vt28-canary-evidence.log`

Summary-only will be bounced.

## Pillar 7

Merge requires Fazal `type: task` with `authorized_by: fazal`.

## Authority

Flip `.viabe/queue/VT-28/status` from `blocked-on-VT-171` → `implementing` and proceed.

Go.
