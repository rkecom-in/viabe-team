---
reviewer: cowork
verdict: APPROVED-with-1-condition
ts: 2026-05-26T14:58:00+05:30
plan_sha: (queue/VT-176/plan.md)
---

# Review — VT-176 plan

**APPROVED with 1 small condition** + both Qs answered.

## Q1 (VT-28 canary disposition) — APPROVED (A) delete it

Same pattern as VT-101 → VT-171 canary deletion. Once you flip shell-emissions to real emissions, VT-28 canary's `*_shell`-event assertions are guaranteed to fail. Don't keep a known-broken canary as ballast. Document the deletion in `pre-merge-result` (cite Group A/B of VT-176 canary as the assertion-of-record for the inherited registration + idempotency + cross-trigger isolation).

## Q2 (monthly-impact 30-day threshold) — Confirmed YES, exactly 30 days

`paid_conversion_at < 30 days ago` skips. Canary fixture at `now - 45 days` is correct (just past the threshold).

## Condition 1 — Migrate VT-28's register_scheduled_triggers idempotency assertion to a pure unit test

VT-28 canary Assertion #10 verified `register_scheduled_triggers` is idempotent (first-call 4 decorations, second-call no-op). That's an architectural invariant, not a runtime-API-needs-real-backend assertion. **Move it to `tests/orchestrator/test_scheduled_triggers.py` as a pure unit test** (no DB, no Anthropic, no DBOS app startup required — just module-level decoration count check). Preserves the regression coverage without keeping a stale canary file.

Done in the same PR as the VT-28 canary deletion; ~10 lines of test code.

## What's right in your plan that I want to acknowledge

- **Risk #3 (apply_transition under scheduled-handler context):** good catch. Verify at PICKUP that calling `apply_transition` from within a `@DBOS.scheduled` workflow doesn't violate DBOS's transactional-boundary assumptions. If it does, surface via `plan-updated`.
- **Body signature `(now: datetime | None = None)` kept consistent with VT-28's surface:** correct. Don't change the public signatures; only swap the body logic.
- **Eligibility scan internal to the body:** correct architectural call. Each scheduled wrapper fans out to per-target invocations.

## Rule #15 audit standard for `pre-merge-result`

- Canary wall-clock + per-assertion observed values (10 assertions)
- PREFLIGHT: Supabase + Anthropic (loaded ONLY for Group D) + Logfire EU + DBOS
- **6-canary regression sweep:** VT-102 (7/7) + VT-103 (8/8) + VT-104 (10/10) + VT-171 (11/11) + VT-28 (DELETED — cite as superseded by VT-176 Groups A/B; OK to skip in sweep) + VT-175 (8/8). So effective sweep = 5 canaries / 44 assertions.
- Anthropic cost < 100 paise (Group D weekly cadence only)
- Sample pipeline_log JSON for each of the 4 real completion event types: `weekly_cadence_fired`, `attribution_closed`, `day39_continue` / `day39_refund_triggered`, `monthly_impact_started`
- Full stdout tail ≥ 150 lines + log at `/tmp/vt176-canary-evidence.log`
- Pure unit test for `register_scheduled_triggers` idempotency (per Condition 1)

Summary-only will be bounced.

## Out of scope (concurred)

- Weekly cadence full supervisor handoff — deferred to post-VT-126 (L0 memory)
- Monthly impact PDF generator — separate Backlog row (VT-9.6 successor)
- Refund execution flow — separate VT-Billing row
- Refund-conversation engine — separate VT-OwnerSurface row

## Pillar 7

Merge requires Fazal `type: task` with `authorized_by: fazal`.

## Authority

Flip `.viabe/queue/VT-176/status` from `review` → `implementing` and proceed.

Go.
