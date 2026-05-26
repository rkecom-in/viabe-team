---
task: VT-102
author: claudecode
ts: 2026-05-25T23:51:44+05:30
estimated_tokens: 170000
estimated_minutes: 130
---

## Approach

Build the append-only structured event store as one logical seam: migration `021_pipeline_log.sql` (next numeric, **not** the brief's `038` — Notion gap, see risk #1) + writer + event-type schemas + query API, all under the existing `apps/team-orchestrator/src/orchestrator/observability/` (extending the VT-101 module, not creating a sibling) + tests + Rule-#15 canary at `apps/team-orchestrator/canaries/vt102_pipeline_log.py`. Reuse `observability/pii.py::redact_for_langsmith` as the redactor (Cowork-recommended Option A — same module both LangSmith and pipeline_log feed, future VT-104 replaces in one place). Writer is async fire-and-forget via `asyncio.create_task` so log calls return immediately; on background-task failure, write a structured stderr breadcrumb and drop the row (Phase-1 spec — no `pipeline_log_failures` sentinel table; that's Phase 2). PayloadSchema validation is soft — invalid payload still writes with a `payload_validation_failed: true` flag in `payload` itself so observability stays best-effort during code drift. RLS pattern follows `015_app_role.sql` / `020_owner_inputs.sql`: `app_current_tenant()` for tenant-scoped SELECT; INSERT allowed via INSERT policy keyed on tenant_id; UPDATE/DELETE blocked at the policy level (no policy → app_role denied; service role bypasses for retention). Retention sweep is a callable function `purge_pipeline_log_older_than(days=90)` invoked under service role; nightly cron wiring is out of scope (Phase 2 per brief).

## File changes

- **NEW `migrations/021_pipeline_log.sql`** — table + 4 indexes + RLS (FORCE) + retention sweep function. Schema matches brief verbatim (id/run_id/tenant_id NULL-allowed/event_type/severity/component/payload JSONB/duration_ms/created_at). Indexes: `(run_id, created_at DESC)`, `(tenant_id, created_at DESC) WHERE tenant_id IS NOT NULL`, `(event_type, created_at DESC)`, `(severity, created_at DESC) WHERE severity IN ('error', 'critical')`. RLS policies: SELECT (tenant_id = app_current_tenant() OR tenant_id IS NULL AND <service role>) — actually NULL-tenant rows visible to service-role only via no app_role grant; tenant rows tenant-scoped. INSERT policy keyed on tenant_id match. No UPDATE/DELETE policies → app_role gets `permission denied`. Service role retains full bypass for retention.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/log.py`** — `log_event(event_type, run_id, tenant_id, severity, component, payload, duration_ms=None)`. Internally: build row dict, run `redact_for_langsmith(payload)`, validate via `event_schemas.validate(event_type, payload)` (soft — annotate `payload_validation_failed`), schedule write via `asyncio.create_task(_async_insert(...))`. The `_async_insert` opens its own connection (via `tenant_connection(tenant_id)` if tenant_id else service connection), executes INSERT, never raises into caller. Stderr breadcrumb on failure.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py`** — `EVENT_SCHEMAS: dict[str, dict]` with the ~14 canonical types from the brief (`webhook_received`, `webhook_signature_verified`, `agent_dispatched`, `tool_invoked`, `tool_completed`, `db_write`, `external_api_call`, `external_api_response`, `error`, `phase_transition`, `scheduled_trigger_fired`, `delivery_attempted`, `payment_event`, `consent_event`, plus `canary_test`). Each schema is a flat dict of `{key_name: type-check-callable}` — no pydantic to keep startup cost low (the writer is on the hot path). `validate(event_type, payload) -> tuple[bool, list[str]]` returns `(ok, error_messages)`.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/query.py`** — four functions per brief. `query_run(run_id)` uses the `(run_id, created_at DESC)` index. `query_tenant_recent(tenant_id, since, limit=100)` uses partial tenant index. `query_errors_recent(since, severity_min='error', limit=100)` uses partial severity index; service-role only — function checks role and raises `PermissionError` if called under app_role (defense in depth on top of RLS). `query_event_type(event_type, since, limit=100)` uses event_type index. Returns `list[PipelineLogEvent]` dataclasses.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/types.py`** — `PipelineLogEvent` dataclass (id, run_id, tenant_id, event_type, severity, component, payload, duration_ms, created_at). Kept in a sibling file so query.py + log.py both import without circular.
- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/__init__.py`** — re-export `log_event`, `query_run`, `query_tenant_recent`, `query_errors_recent`, `query_event_type`, `purge_pipeline_log_older_than`, `PipelineLogEvent`.
- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/pii.py`** — extend `redact_for_langsmith` (NOT renaming for VT-101 stability) with two extra fields needed by pipeline_log: `stack_trace` (TEXT body-style redaction) and `error_message` (token-keep). Add a re-exported alias `redact_for_log = redact_for_langsmith` so call sites in `log.py` read clearly without coupling to LangSmith naming.
- **NEW `apps/team-orchestrator/tests/orchestrator/observability/test_pipeline_log.py`** — pytest. Six suites:
  1. Append-only enforcement (UPDATE / DELETE under app_role → permission denied).
  2. PII redaction at write (synthesise payload, mock async insert capture, assert payload after redact-and-serialize contains no raw PII).
  3. Query API: 50 synthesised events for one run_id → `query_run` returns chronologically ordered.
  4. Cross-tenant: tenant_A insert → tenant_B query returns empty; tenant_A query returns 1.
  5. Workspace-level (tenant_id NULL): not visible under app_role; visible under service role.
  6. Schema validation: invalid payload → row written with `payload_validation_failed: true`; valid payload → flag absent.
  Tests use `pytest.mark.integration` (already in conftest, skipped unless `RUN_INTEGRATION_TESTS=1` set) for the DB-backed ones; the pii-redaction + schema-validation tests are pure (no DB) and run unconditionally.
- **NEW `apps/team-orchestrator/canaries/vt102_pipeline_log.py`** — Rule #15 canary, 7 assertions per brief §"Canary (Rule #15 — mandatory)". Subshell-source `.viabe/secrets/supabase-dev.env`. Uses a fixed pair of test tenant UUIDs (constants in the script, prefixed `vt102-canary-`) to keep the data identifiable + cleanable. Script cleans up its own writes via a `DELETE WHERE run_id IN (...)` under service role at exit (best-effort, doesn't gate the canary result).
- **NEW `apps/team-orchestrator/canaries/README.md`** — already exists from VT-101; no change.

## Test plan

Six pytest cases (integration-gated where DB-backed). Local validation chain mirrors VT-101:
- `pytest tests/orchestrator/observability/ -v` — covers all six cases; integration cases skip on no `RUN_INTEGRATION_TESTS=1`.
- `RUN_INTEGRATION_TESTS=1 DATABASE_URL=... pytest tests/orchestrator/observability/ -v` — full run against local Postgres (the `orchestrator` CI job pattern).
- `ruff check src/orchestrator/observability` — clean.
- `python -m mypy --strict src/orchestrator/observability/` — clean.
- Apply migration locally + run canary against dev DB before opening PR: `( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a; ./.venv/bin/python canaries/vt102_pipeline_log.py )` — expect 7/7 PASS. Audit JSON gets pasted into the `pre-merge-result` signal body.

## Risks

1. **Migration number — brief says `038`, repo only has up to `020`.** Same Notion-projection gap that caused VT-101's `apps/team/` paths. Real next number is `021`. I'll surface the rename to Cowork at plan-ready; no escalation since the schema is what matters and the file-number is just an ordering key.

2. **Brief artifacts (same class as VT-101).** PR title `(VT-Observability-Cost)` fails the regex; will use `(VT-102)`. `dev` merge target doesn't exist; targets `main`. CoderC/CoderX retired; reviewers skipped per CL-151. Paths `apps/team/` → `apps/team-orchestrator/`. All addressed in this plan; surfaced in plan-ready.

3. **Async fire-and-forget vs DBOS.** The orchestrator uses DBOS for durable workflows. A `log_event` call from inside a `@DBOS.step` body that schedules `asyncio.create_task` may interact awkwardly with DBOS's own asyncio loop. Mitigation: the writer detects whether it's inside a running loop (`asyncio.get_running_loop()` try/except) — if yes, schedule a task; if no, run inline-async (sync wrapper using `asyncio.run` on a thread). Either way the call returns to caller fast. Documented in the writer docstring.

4. **Retention sweep semantics.** Brief says "90 days; nightly delete." Phase-1 deliverable per the out-of-scope is the FUNCTION + tests, NOT the cron. The function `purge_pipeline_log_older_than(days=90)` is service-role-only (raises `PermissionError` under app_role; the RLS path is belt + suspenders — service role bypasses RLS but the function check is also present so a misconfigured caller isn't silently no-op). Wiring the cron is a separate VT row (Phase 2 / Cowork's call).

5. **PII redactor scope creep.** Adding `stack_trace` + `error_message` field handlers to `pii.py` widens the shared utility's surface. Acceptable because VT-104 subsumes it later; the call-site contract (`redact_for_langsmith(value)` / `redact_for_log(value)` alias) stays stable. If Cowork prefers a duplicated inline redactor at `log.py` to keep `pii.py` LangSmith-only, surface as a review condition; I'll comply.

6. **Schema validation runtime cost.** `EVENT_SCHEMAS` is a static dict with callable validators — O(k) where k = number of keys in payload, typically <10. Validates at write time. Adds maybe 50µs per `log_event` call. Acceptable for a logging path.

7. **Canary cleanup safety.** The canary writes ~106 rows (1 base + ~100 indexed-query + 1 PII + 1 cross-tenant + 3 retention). At exit, runs `DELETE FROM pipeline_log WHERE run_id IN (<canary-prefixed UUIDs>)` under service role. Cleanup is best-effort — if it fails, the rows have a recognisable `component='canary'` and tenant_id = canary tenant; they get swept by the 90-day retention anyway. The canary's own assertions don't depend on cleanup.

8. **Token budget.** Est 170K vs Cowork's 180K ceiling. Tight but in. If implementation drifts >180K I'll split: PR1 (migration + writer + schemas + query + pure tests + canary) shipped immediately; PR2 follow-up = the integration-DB tests + any wire-up to existing runner.py. Single-PR is the strong preference per VT-101 lesson; will surface mid-flight if I see budget burn.
