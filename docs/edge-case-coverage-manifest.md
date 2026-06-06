# Edge-case coverage manifest (VT-107)

**What this is.** The VT-107 spec (May-06) asked for a from-scratch 100–200 scenario `edge_cases/`
suite + a CI-blocking fast/slow tier. That is **superseded by shipped reality**: the repo already
carries **228 test files** (181 pytest in `apps/team-orchestrator/tests/`, 47 vitest in
`apps/team-web`) + **11 structural CI Pillar gates** + a Playwright E2E job, and **VT-245 (standing)
turned CI status-checks OFF — the pre-push hook is the merge gate**, not a CI tier. So VT-107 is
re-scoped (Cowork 20260606T224500Z, option a) to this **manifest + gate-alignment + a thin
gap-fill**, not a parallel suite (which would duplicate ~228 tests + re-litigate VT-245).

This manifest is the index: it maps the existing coverage to the spec's failure-mode categories and
the Pillars, distinguishes **canary-grade** from **unit-grade**, and lists the genuine gaps.

## Grades

- **Canary-grade** — exercises a REAL dependency: real Postgres (a `substrate` / `_dbpool` fixture
  → `DATABASE_URL`, `apply_migrations`), real migration apply, or a real API; **fail-not-skip** at
  the boundary (Rule #15). This is the strong green — it would catch a real regression.
- **Unit-grade** — mocks / `monkeypatch` / stubs, no real dependency. Useful for logic, but a
  category covered ONLY by unit-grade is a **softer green** (it can pass while the real seam breaks).

## Coverage by failure-mode category

| # | Category | Representative canary-grade files | Grade lean |
|---|----------|-----------------------------------|------------|
| 1 | i18n / classifier intents (Devanagari, negation, opt-out/DSR) | `test_pre_filter.py`, `test_pre_filter_i18n_pure.py`, `test_customer_inbound.py` (VT-358), `test_dsr_purge_substrate.py` | mostly canary |
| 2 | Cross-tenant isolation / RLS | `test_wrappers_rls.py`, `test_rls_set_config_realdb.py`, `test_tenant_isolation.py`, `test_customer_inbound.py` (cross-tenant) | strong canary |
| 3 | Concurrent races / idempotency | `test_razorpay_subscribe.py` (advisory lock + commit-after-vendor retry), `test_razorpay_ingress.py` (dedup), `test_founding_counter.py`, `test_owner_inputs_idempotent` (VT-149) | mostly canary |
| 4 | Malformed / poison inputs | `test_razorpay_ingress.py` (parse-drop + replay, VT-330/352), `test_twilio_ingress.py` (HMAC), `test_customer_inbound.py` | mostly canary |
| 5 | PII redaction / consent | `test_pii_redactor.py`, `test_k_anonymity.py`, `test_kg_pii_strip.py`, `test_runner_body_redaction.py`, `test_consent_substrate.py` | mostly canary |
| 6 | Hard-limit / budget edges | `test_sales_recovery.py` (token cap), `test_trial_evaluator.py`, `test_day39_evaluator.py` + the `gate-vt35-hard-limit-constants` gate | canary + gate |
| 7 | Timeout / retry / external API | `test_vision_extraction.py`, `test_voice_transcription.py`, `test_whatsapp_account.py` (fail-closed send-gate), `test_refund_executor.py` | mixed |
| 8 | Billing / money | `tests/orchestrator/billing/*` — `test_razorpay_ingress.py`, `test_razorpay_subscribe.py`, `test_refund_executor.py`, `test_trial_evaluator.py`, `test_attribution_close.py`, `test_dispatch_guard_realdb.py` (VT-328) | strong canary (145/145) |
| 9 | Owner surface / edge cases | `test_edge_cases.py`, `test_edge_cases_pr2.py` (VT-84/336), `test_monthly_report*.py`, `test_escalations_substrate.py` (VT-357 SLA) | canary, **thin** — see GAP-1 |
| 10 | Scheduled triggers / sweeps | `test_scheduled_triggers.py` (register-count), `test_vt305_pii_log_sweep.py`, `test_vt307_kg_drain_sweep.py`, `test_vt311_l2_retention.py`, `test_escalations_substrate.py` | mostly canary |
| 11 | Frontend / web | `apps/team-web/**/*.test.ts(x)` (47, vitest) | unit by design — see GAP-2 |

## The 11 structural Pillar gates (`.github/workflows/ci.yml`)

These enforce invariants no per-test scenario can (whole-tree greps / schema checks). **Guarded
against silent removal by `tests/test_pillar_gates_present.py`** (VT-107 gap-fill — the list there
is the forcing function).

| Gate | Enforces |
|------|----------|
| `gate-no-deprecated-langgraph-imports` | no `create_react_agent` |
| `gate-no-price-literals` | prices only from config (Pillar 7) |
| `gate-no-llm-in-deterministic-triggers` | no LLM in deterministic triggers + `billing/` (Pillar 1) |
| `gate-no-langsmith-imports` | Logfire, not LangSmith (CL-56) |
| `gate-no-direct-tenant-db-access` | tenant SQL only via `orchestrator.db.wrappers` (RLS; VT-72/306/324) |
| `gate-sr-agent-prompt-token-cap` | sales-recovery prompt ≤ token cap (VT-33) |
| `gate-vt39-tools-harness-import` | every tool test imports `run_tool_test` (VT-39) |
| `gate-connector-registry-schema` | connector specs validate vs `ConnectorSpec` (VT-205) |
| `gate-vt35-hard-limit-constants` | Type-3 limits = tokens 80k / tools 25 / depth 8 / wallclock 300s (VT-35) |
| `gate-langgraph-nodes-have-observability-hook` | every `add_node` wrapped (VT-186/183) |
| `gate-mcp-tools-have-observability-decorator` | every `@tool` has `@observability.tool_step` (VT-186/181) |

## Gate alignment — "failures block merge"

The spec's "CI blocks merge" intent is met by the **pre-push hook** (`scripts/git-hooks/pre-push`),
NOT a CI tier (VT-245 standing: CI status-checks OFF, CI is a non-blocking backstop). The hook runs,
on every push: `ruff` (orchestrator + ingestion-worker) → the **dep-less smoke** `pytest` (mirrors
the CI `test` job; catches importorskip-guard misses) → team-web `tsc` + `vitest` + `lint` → a
conditional orchestrator docker build. A failing edge-case test there **aborts the push**. The 11
gates + the Playwright `e2e-playwright` job run in CI as the backstop. No new fast/slow tier is
added (it would re-litigate VT-245).

## Gaps (anti-proliferation — fill small here, roster/accept the rest)

- **GAP-1 — owner-surface edge cases (ROSTER-OR-ACCEPT, Cowork):** category 9 is the thinnest
  canary set (`test_edge_cases.py` + `_pr2.py` + monthly-report + the VT-357 SLA substrate). Missing
  edge cases worth a row IF wanted: approval-request retry/timeout races, DSR-request malformed-field
  handling, dashboard state races. This is a category EXPANSION (>~1h, multiple substrate tests) —
  **not** silently built here per anti-proliferation. Cowork: roster a follow-up VT row, or accept
  the current depth (the owner surface is also covered indirectly via cats 3/4/5/10).
- **GAP-2 — frontend all-unit (ACCEPT):** the 47 vitest files are unit-grade (mocks); this is by
  design — real frontend E2E is the `e2e-playwright` CI job (chromium + next build). No action.

## Filled here (the thin gap-fill, tests+docs only)

- `tests/test_pillar_gates_present.py` — guards the 11 Pillar gates from silent deletion (the gates
  are load-bearing for several categories' coverage claims; a deleted gate must fail a test, not
  pass silently). Dep-less (runs in the pre-push smoke + CI `test`).
