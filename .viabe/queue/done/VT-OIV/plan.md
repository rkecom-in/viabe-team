---
task: VT-OIV
author: claudecode
ts: 2026-05-24T16:25:00+05:30
estimated_tokens: 80000
estimated_minutes: 55
---

## Approach

Add the three behavioral tests the brief requires, the env-gated real-Anthropic canary, and the migration-comment cite fix. Two tests extend `test_twilio_ingress.py` (which already owns the webhook-pipeline E2E pattern with the `ingress` fixture); the DSR test extends `test_dsr_purge_substrate.py` (which already seeds tenants + opens tickets); the canary lives in a new file mirroring `test_sales_recovery_end_to_end.py`'s triple-env-gate pattern (`RUN_INTEGRATION_TESTS=1` + `ANTHROPIC_API_KEY` + `DATABASE_URL`). Flag-flip is `monkeypatch.setattr(runner_mod, "OWNER_INPUTS_EXTRACTION_ENABLED", True)` only — the constant in `main` stays False (per brief hard-rule + existing SHIP-GATE test). `classify_message` is patched at the writer module (`orchestrator.owner_inputs.writer.classify_message`) so the production code path through `run_extraction_for_event` is exercised end-to-end without the real API.

## File changes

| File | Change |
|---|---|
| `migrations/020_owner_inputs.sql` | Replace retention-comment cite `368387c2-cc5a-8180-be0b-d1e64e0366de` (superseded) with `368387c2-cc5a-8162` (authoritative). Comment-only; no SQL change. |
| `apps/team-orchestrator/tests/orchestrator/test_twilio_ingress.py` | Add `test_owner_inputs_extraction_writes_structured_row` and `test_ingress_resilient_on_classifier_failure`. |
| `apps/team-orchestrator/tests/orchestrator/test_dsr_purge_substrate.py` | Add `test_owner_inputs_dsr_purge_covers_substrate` — focused per-feature assertion (the existing `_PURGED_TABLES` sweep covers owner_inputs already, but the brief asks for an explicit row for it). |
| `apps/team-orchestrator/tests/orchestrator/test_owner_inputs_canary_real_anthropic.py` (new) | Env-gated canary: real Anthropic + real DB + real flag-flip → one real classification + write + assertions on the row. |

No production-code edits (the writer + runner + dsr_purge are correct per Step-0; the brief only asks for verification + the migration-comment fix).

## Test plan

All three new tests are behavioral — they invoke the production code path (FastAPI `TestClient` → `webhook_pipeline_run` → `run_extraction_for_event` → `write_owner_input`; or `purge_tenant_data` against a real seeded tenant). None use `inspect.getsource` or copy production transforms into the test. Each follows the conventions already established in the neighbouring test files (module-scope `substrate`/`ingress` fixture, `_new_tenant` helper, `psycopg.connect` for direct read-back).

### 1. `test_owner_inputs_extraction_writes_structured_row` (extends `test_twilio_ingress.py`)

- `monkeypatch.setattr(runner_mod, "OWNER_INPUTS_EXTRACTION_ENABLED", True)`.
- `monkeypatch.setattr("orchestrator.owner_inputs.writer.classify_message", lambda body, client=None: OwnerInputClassification(intent="winback", segment="dormant_60d", occasion="diwali"))` — avoids the Anthropic API key dep; production path otherwise untouched.
- `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sentinel")` so `run_extraction_for_event`'s early-skip on missing key does not short-circuit.
- POST one webhook with a unique `secret_body = f"REDACT-PROBE-{uuid4().hex}-message"` and `MessageSid`, `_await_workflow`, then read back:
  - `owner_inputs` row scoped to the tenant: exactly 1 row; `intent='winback'`, `segment='dormant_60d'`, `occasion='diwali'`, `message_sid == sid`, `run_id == result["run_id"]`, `consumed_at IS NULL`, `created_at` not null and recent.
  - `row_to_json(owner_inputs.*)::text` does NOT contain `secret_body` (defence in depth — covers brief item 3 sink 1).
  - `pipeline_runs.trigger_payload` has no `body` key and does not contain `secret_body` (sink 2).
  - `pipeline_steps.input_envelope` for the `webhook_received` step has no `body` key and does not contain `secret_body` (sink 3).
  - Sink 4 (`dbos.workflow_status.inputs`) is accepted per CL-385 / VT-150-fix-1 with a 2.5h GC. The test does NOT assert on it; an inline comment cites CL-385 + the existing `test_dbos_layer_not_synchronously_purged_documented_finding` invariant so a future reader knows where the boundary lives.

### 2. `test_ingress_resilient_on_classifier_failure` (extends `test_twilio_ingress.py`)

- Flag on (`monkeypatch.setattr`), `ANTHROPIC_API_KEY` set to a sentinel.
- `monkeypatch.setattr("orchestrator.owner_inputs.writer.classify_message", _boom)` where `_boom` raises `RuntimeError("simulated classifier failure")`. This exercises the `run_extraction_for_event`'s outer `try/except` (the writer's contract per its docstring).
- POST one webhook with a substantive owner message (so the pre-filter routes to `brain` and we get the deterministic `escalated` terminal — proves the rest of the pipeline ran).
- Assert: HTTP 200; `_await_workflow` returns without exception; `pipeline_runs.status == 'escalated'`; the `webhook_received` step record exists; the `awaiting_brain` step record exists; `owner_inputs` rows for the tenant count == 0; no 5xx logged.

### 3. `test_owner_inputs_dsr_purge_covers_substrate` (extends `test_dsr_purge_substrate.py`)

- `_seed_full_tenant_data` already seeds an `owner_inputs` row (line 184-189). New test:
  - Seed one tenant + N=3 owner_inputs rows (varied `consumed_at`: 1 null + 2 set, to prove the purge does not depend on the pending filter).
  - Open a deletion ticket via `_open_dsr_ticket`.
  - `purge_tenant_data(ticket_id)`.
  - Assert `_count_tenant_rows(dsn, "owner_inputs", tenant_id) == 0`.
  - Assert `result.deleted_counts["owner_inputs"] == 3`.
  - Cross-tenant non-leak: seed a second tenant with its own owner_inputs row, purge only A, assert B's row count is still 1 (reuses the `_PURGED_TABLES` cross-tenant pattern but with focused assertions for owner_inputs).

### 4. Canary — `test_owner_inputs_canary_real_anthropic.py` (new, env-gated)

- `pytestmark = [pytest.mark.integration, pytest.mark.skipif(...)]` mirroring `test_sales_recovery_end_to_end.py`: requires `RUN_INTEGRATION_TESTS=1` + `ANTHROPIC_API_KEY` + `DATABASE_URL`. CI skips; Fazal runs once pre-merge.
- Seed tenant; flag on via monkeypatch; `_LedgerClient`-style wrap around the real `Anthropic` SDK to capture the call (proof-of-call per CL-272: model id, first user message ≤200 chars, `response.id` starts with `msg_`).
- Send one representative owner message: e.g. `"Plan a Diwali campaign for dormant customers."`
- Assert: `OwnerInputClassification.intent in _ALLOWED_INTENTS`, segment/occasion are strings or None, exactly one row in `owner_inputs`, `row_to_json` does not contain the message text.
- The PR description will call out that Fazal must run this manually pre-merge with `RUN_INTEGRATION_TESTS=1 ANTHROPIC_API_KEY=… DATABASE_URL=… pytest tests/orchestrator/test_owner_inputs_canary_real_anthropic.py -v`.

### Existing tests preserved

`test_owner_inputs_substrate.py` (6 tests), `test_owner_inputs_ship_gate.py` (2 tests), `test_dsr_purge_substrate.py` (existing 5 tests), `test_twilio_ingress.py` (~20 tests) — all kept untouched. The SHIP-GATE test (`test_owner_inputs_extraction_default_is_off`) is the binding lock that the constant on `main` stays False.

### Run order

1. Local: `pytest apps/team-orchestrator/tests/orchestrator/owner_inputs/ apps/team-orchestrator/tests/orchestrator/test_owner_inputs_ship_gate.py -v` (sanity).
2. Local: `pytest apps/team-orchestrator/tests/orchestrator/test_twilio_ingress.py -v -k "extraction or resilient"` (new tests).
3. Local: `pytest apps/team-orchestrator/tests/orchestrator/test_dsr_purge_substrate.py -v -k "owner_inputs"` (new test).
4. Local: `pytest apps/team-orchestrator/tests/orchestrator/test_owner_inputs_canary_real_anthropic.py -v` — should `SKIPPED` without env vars.
5. CI: `orchestrator` job runs everything except the canary; pre-existing job picks up the 3 new behavioural tests automatically.

## Risks

- **Monkeypatching `OWNER_INPUTS_EXTRACTION_ENABLED` in a `@DBOS.workflow`-decorated function**: the constant is read at call-time inside `webhook_pipeline_run`, not captured at decorator-application, so `monkeypatch.setattr(runner_mod, "OWNER_INPUTS_EXTRACTION_ENABLED", True)` works. Confirmed by re-reading `runner.py:294` — the read is `if OWNER_INPUTS_EXTRACTION_ENABLED:` inside the function body. Will validate with a quick `pytest -x` early.
- **Symbol patching at the writer module vs. the import in `runner.py`**: `runner.py` does `from orchestrator.owner_inputs import run_extraction_for_event`, which means `runner.run_extraction_for_event` is the name to patch if you want to replace the whole call. But I want to drive the *real* `run_extraction_for_event` (so its outer try/except runs); I only need to swap `classify_message`. The writer calls `classify_message` via a direct module-local name (`classification = classify_message(event.body, client=client)`) — patching `orchestrator.owner_inputs.writer.classify_message` works because Python resolves the name in the module's namespace at call time. (Cross-checked via `__init__.py` re-export which is read-only.) The substrate test file already uses similar patches.
- **DBOS workflow result observability**: `_await_workflow` blocks until completion; if the workflow raises, it re-raises. For the resilience test the workflow MUST NOT raise (the writer swallows). If the brief's contract were ever broken — i.e., a future edit moves the call outside the writer's try/except — the test would fail loud with the exception trace. That is the right signal.
- **Migration comment fix vs. CI re-apply**: changing a SQL comment in `020_owner_inputs.sql` is content-only (no DDL change). CI's `apply_migrations.apply` is idempotent (`CREATE TABLE IF NOT EXISTS`-style or migration-ledger gated) — but I will verify the apply path is idempotent on a pre-existing table before relying on it; if the migrations runner re-issues the `CREATE TABLE` on every boot, the comment-only diff still must not break a fresh apply. (Quick read of `apply_migrations.py` planned at implementation time.)
- **Canary cost / time**: one Haiku turn per run, sub-cent per call. Acceptable per the brief's own estimate.
- **No production-code changes**: this PR is verification + one comment fix. The SHIP-GATE constant stays False on `main` (brief hard-rule). The flag-flip is Fazal's separate action post-merge.

## Out of scope (explicit)

- Flipping `OWNER_INPUTS_EXTRACTION_ENABLED` to True on `main` (Fazal's call post-merge).
- Any changes to the writer's classification logic, prompt, or model.
- DBOS retention reduction (CL-385 standing lock).
- Per-tenant attribution recovery-target wiring, approved-templates registry (separate VT rows).
