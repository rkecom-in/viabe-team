---
reviewer: cowork
verdict: APPROVED-both-questions-with-1-note
ts: 2026-05-26T13:38:00+05:30
plan_sha: (queue/VT-175/plan.md)
---

# Review — VT-175 plan

**APPROVED both Q1 + Q2.** Plan is clean, narrow, well-scoped. One catch you found that I need to acknowledge: my brief had a transcription error on the RLS GUC convention.

## Q1 (RLS GUC convention) — APPROVED `app_current_tenant()` helper

Use `app_current_tenant()` per `migrations/000b_rls_helpers.sql`. Matches every other RLS-bound table on main + works with the existing `tenant_connection()` Python wrapper. **Substrate consistency wins.**

The brief's example used `current_setting('app.tenant_id')::uuid` because I transcribed CL-82's literal wording from `docs/clau/decisions-ledger.md` without ground-truthing against `000b_rls_helpers.sql`. CL-82's *spirit* — "RLS via session GUC, NOT auth.jwt()" — is preserved by `app_current_tenant()`. **The Standing decision's wording is drifted from the substrate; I just propagated the drift.** This is a Rule #16-adjacent failure: the ledger check surfaces the decision, but doesn't ground-truth that the literal example still matches code.

**Cowork action post-VT-175 merge:** add a "substrate note" to `docs/clau/active-context-summary.md` for CL-82 — *"Active implementation: `app_current_tenant()` helper in `migrations/000b_rls_helpers.sql` reading `app.current_tenant` GUC. Standing decision text references `app.current_tenant_id`; substrate is the source of truth."* That way the next brief author (Cowork or otherwise) doesn't repeat the transcription error.

**No condition needed** on you for this — your plan already uses the helper; just confirming the call.

## Q2 (extend `gate-no-llm-in-deterministic-triggers` to billing/) — APPROVED YES

3-line gate extension; complementary to canary Assertions #5 + #8 (runtime zero-LLM verification). Pillar 1 structural enforcement at code level. Same philosophy as VT-171's `gate-no-langsmith-imports` + VT-28's original `gate-no-llm-in-deterministic-triggers`.

Scope addition: add `apps/team-orchestrator/src/orchestrator/billing/*.py` to the gate's grep path. Forbidden patterns stay the same (`ChatAnthropic|Anthropic|claude-|langchain_anthropic|orchestrator_agent|supervisor|messages.create|llm`).

## Other items in your plan that I want to acknowledge explicitly

- **Risk #2 (VT-28 reserved-event-names ownership)** — correct. The 3 event-type schemas (`attribution_closed`, `day39_continue`, `day39_refund_triggered`) get REGISTERED here in `event_schemas.py` but no production code path emits them yet — those bodies ship in VT-176. This is the inverse of the "shells emit shell events" pattern: schemas land first, then bodies. Good sequencing.
- **Risk #4 (idempotency under concurrent close)** — your `UPDATE … WHERE attribution_closed_at IS NULL RETURNING 1` atomic-per-row approach is correct. The canary's Assertion #4 verifies this.
- **Risk #6 (DDL transactional)** — single BEGIN/COMMIT for the migration. Standard.
- **5-canary regression sweep ~175s** — acknowledged that this is in `pre-merge-result` audit time, not canary wall-clock. Same pattern as VT-28 pre-merge-result.

## Rule #15 audit standard for `pre-merge-result`

Verbatim required:
- Total VT-175 canary wall-clock (`time` output)
- Resolved Supabase host at PREFLIGHT (credentials stripped); confirm Anthropic env var ABSENT in canary loader
- Per-assertion observed values for all 8
- **Plus 5-canary regression sweep**: VT-102 (7/7) + VT-103 (8/8) + VT-104 (10/10) + VT-171 (11/11) + VT-28 (10/10) byte-identical against current main
- **Anthropic cost = 0 paise captured** (structural; no anthropic.env source)
- One sample pipeline_log row for each new event type (verbatim JSON) — `attribution_closed`, `day39_continue`, `day39_refund_triggered`
- Full stdout tail ≥ 150 lines + full log at `/tmp/vt175-canary-evidence.log`

Summary-only will be bounced.

## Out of scope (concurred)

- Shell-body replacement in `scheduled_triggers.py` — VT-176 scope
- `customers` table — VT-170 follow-up (`customer_id` stays NULLABLE in `attributions`)
- Refund execution flow + refund-conversation engine — separate VT-Billing / VT-OwnerSurface rows
- Per-tenant retention policies — Phase 2

## Pillar 7

Merge requires Fazal `type: task` with `authorized_by: fazal`.

## Authority

Flip `.viabe/queue/VT-175/status` from `review` → `implementing` and proceed.

Go.
