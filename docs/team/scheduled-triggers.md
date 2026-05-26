# Scheduled triggers (VT-28)

Four DBOS `@DBOS.scheduled` cron workflows registered at app startup via
`scheduled_triggers.register_scheduled_triggers()` (mirrors `dbos_purge`
register-before-launch pattern).

## Cron table (Phase 1 IST-only; per-tenant TZ is Phase 2)

| # | Trigger | Cron (IST) | LLM? | Workflow ID derivation |
|---|---------|------------|------|------------------------|
| 1 | Weekly cadence | `0 9 * * MON` (Mon 9 AM) | **YES** (orchestrator-agent direct, Option A) | `weekly:{tenant_id}:{iso_week}` |
| 2 | Attribution close | `0 2 * * *` (daily 2 AM) | NO | `attribution_close:{campaign_id}` |
| 3 | Day-39 evaluation | `0 6 * * *` (daily 6 AM) | NO | `day39:{tenant_id}` |
| 4 | Monthly impact | `0 8 1 * *` (1st 8 AM) | NO | `monthly:{tenant_id}:{YYYY-MM}` |

DBOS exactly-once semantics handle idempotency via the workflow ID —
no separate idempotency table (CL-36, brief §5).

## Plumbing-mode caveat (CL-274)

**VT-28 proves the weekly cadence trigger fires + reaches Anthropic; it
does NOT prove the cadence produces useful output.**

The 3 deterministic triggers (attribution close, day-39, monthly
impact) ship as **SHELLS** in this row pending the schema VT-175 will
deliver. Each emits a `*_shell` event with `status:
skipped_schema_pending`:

| Shell event | Reserved completion event (VT-176) |
|-------------|------------------------------------|
| `attribution_close_shell` | `attribution_closed` |
| `day39_shell` | `day39_evaluated`, `day39_continue`, `day39_refund_triggered` |
| `monthly_impact_shell` | `monthly_impact_started` |

**Phantom-Done prevention (CL-318 / CL-319 / CL-380):** the reserved
completion event names are NOT emitted from `scheduled_triggers.py` in
this row. Future readers MUST NOT interpret a green VT-28 canary as
"the deterministic triggers work" — they only prove the cron registers,
fires, and emits an observable shell event.

## Pillar 1 — deterministic vs reasoning split

Pillar 1 (revised 2026-05-12) separates orchestrator-agent reasoning
paths from deterministic SQL paths. The 3 deterministic trigger bodies
MUST NOT invoke any LLM, import any agent / supervisor module, or
reference `ChatAnthropic` / `claude-` / `langchain_anthropic`.

Structural enforcement: CI gate
`gate-no-llm-in-deterministic-triggers` greps the function bodies for
forbidden tokens. Failures block merge. Parallel to VT-171's
`gate-no-langsmith-imports`.

The weekly cadence trigger is exempt — it owns the reasoning path.

## Pillar 8 — one scheduler substrate

DBOS handles cron + idempotency natively. No external scheduler (no
`apscheduler` / `n8n` / parallel cron container). No custom
idempotency table — DBOS workflow_id is the idempotency key.

Adding a new trigger = a new `@DBOS.scheduled` function + an entry in
`register_scheduled_triggers()`. No other surface to learn.

## Synthetic clock injection (tests + canary)

DBOS scheduled functions fire on real cron; there is no documented
test-clock API. The trigger body functions (`run_*_body`) accept an
optional `now: datetime | None = None` so tests + the canary can
invoke them directly with a synthesised UTC timestamp.

```python
from orchestrator.scheduled_triggers import run_weekly_cadence_body
from datetime import datetime, timezone

# Synthetic Monday 9 AM IST = 03:30 UTC
synthetic = datetime(2026, 5, 25, 3, 30, tzinfo=timezone.utc)
run_id = run_weekly_cadence_body(now=synthetic)
```

The production scheduled wrapper (`weekly_cadence_scheduled`) passes
`actual_time` (the real fire time DBOS reports) through to the body.

## Out of scope in VT-28 (Cowork concurred)

- The schema migration (`attributions` table + `tenants.paid_conversion_at`
  + `campaigns.attribution_*` columns) → VT-175 (Sprint 1, Critical,
  critical path; Cowork files post-VT-28-merge)
- Replacing the shells with real bodies → VT-176 (Sprint 1, Critical)
- Full supervisor + sales_recovery handoff for weekly cadence → post-
  VT-126 (L0 memory + tenant context)
- Per-tenant timezone handling → Phase 2
- Manual trigger fire for testing → use DBOS testing harness or the
  `run_*_body(now=...)` callable directly

## Files

- `apps/team-orchestrator/src/orchestrator/scheduled_triggers.py` — registrations + bodies
- `apps/team-orchestrator/src/main.py` — calls `register_scheduled_triggers()` before `launch_dbos()`
- `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py` — registers 4 new event types
- `apps/team-orchestrator/tests/orchestrator/test_scheduled_triggers.py` — 16 pure tests
- `apps/team-orchestrator/canaries/vt28_scheduled_triggers.py` — 10-assertion real-DBOS + real-Anthropic + real-Logfire canary

## References

- CL-22 / CL-27 (DBOS adoption)
- CL-36 (Standing: DBOS is the substrate)
- CL-56 (Standing: Logfire is the observability backend; VT-171 hot-fix)
- CL-274 (plumbing-mode discipline)
- CL-318 / CL-319 / CL-380 (phantom-Done prevention; reserved completion event names)
- VT-101 / VT-102 / VT-103 / VT-104 / VT-171 (observability quartet)
- VT-122 (`register_purge_scheduler` precedent)
