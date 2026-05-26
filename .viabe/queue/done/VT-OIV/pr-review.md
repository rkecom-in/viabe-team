---
task: VT-OIV
reviewer: cowork
ts: 2026-05-24T11:32:00Z
verdict: APPROVED-for-merge
pr_url: https://github.com/rkecom-in/viabe-team/pull/53
branch: test/vt-oiv-owner-inputs-verification
head_sha: c19972f
base_sha: de8c0c1
---

# PR review — VT-OIV (verification before owner_inputs flag-flip)

## Verdict: APPROVED for Fazal merge — with two pre-merge conditions (both Fazal-actions)

## What I verified on the ground

1. **Branch + commit exist.** `git log origin/test/vt-oiv-owner-inputs-verification` shows `c19972f test(owner_inputs): verification before flag-flip (VT-OIV)` cleanly on top of `de8c0c1` (PR #52, current main). 4 files changed, 469 insertions / 3 deletions.

2. **SHIP-GATE binding lock held.** `OWNER_INPUTS_EXTRACTION_ENABLED = False` at `runner.py:35` on the branch HEAD — confirmed via `git show c19972f:.../runner.py`. The constant is monkeypatched per-test only; main stays gated OFF. Brief hard-rule satisfied.

3. **All 7 brief goals covered:**
   - Goals 1-3 (classify → write structured row → no raw body in 3 sinks): `test_owner_inputs_extraction_writes_structured_row` — verified via direct `psycopg` SELECTs against `owner_inputs`, `pipeline_runs.trigger_payload`, `pipeline_steps.input_envelope`; unique `secret_body = OWNER-INPUT-PROBE-<uuid>-msg` substring check in all three sinks. Sink 4 (DBOS) correctly left as accepted-per-CL-385 with inline citation.
   - Goal 4 (Composer read-path): closed via the **preferred path** — appended `_build_pending_owner_inputs(_UUID(tenant_id))` assertion at the end of the extraction test (test_twilio_ingress.py:594-602). Returns 1 row matching the just-written intent/segment/occasion. No separate cite signal needed.
   - Goal 5 (DSR purge covers owner_inputs): `test_owner_inputs_dsr_purge_covers_substrate` — seeds `tenant_alpha` with 3 rows (mixed `consumed_at`: 1 NULL + 2 set) and `tenant_bravo` with 1 row. Asserts `deleted_counts["owner_inputs"] == 3`, alpha count → 0, bravo count stays at 1. The mixed-consumed_at seed is the right pin: a future regression scoping the purge to `consumed_at IS NULL` only would fail loud here.
   - Goal 6 (ingress resilient on classifier failure): `test_ingress_resilient_on_classifier_failure` — `classify_message` patched to raise `RuntimeError`; webhook returns 200, status reaches `escalated`, `webhook_received` + `awaiting_brain` steps both written, owner_inputs count = 0.
   - Goal 7 (migration comment fix): `migrations/020_owner_inputs.sql` diff — cite changed to authoritative `368387c2-cc5a-8162` with the superseded `368387c2-cc5a-8180-be0b-d1e64e0366de` retained as a historical note. Comment-only; SQL DDL untouched.

4. **Behavioral, not source-grep.** All 3 new tests use real `psycopg.connect` writes/reads against the test Postgres; the canary uses `_LedgerClient` to wrap the real Anthropic SDK. No `inspect.getsource` in the new tests (the one `inspect.getsource` in `test_dsr_purge_substrate.py` is in the pre-existing dbos-purge test at line 410, untouched by this PR).

5. **Soft notes from plan-review both addressed:**
   - Resilience test docstring cites `orchestrator/owner_inputs/writer.py:240` (`run_extraction_for_event`) at line 607 — a future reader will know what contract the test is guarding.
   - DSR test uses distinctly-named `tenant_alpha` / `tenant_bravo` (18 references) so cross-tenant non-leak failures will be readable.

6. **Pillar compliance + standing locks held.** No body column added. No change to `writer.py`'s body-to-Anthropic transmission (CL-385). No change to DBOS retention. No production-code edits beyond the migration comment. CL-385 + Rule #14 disciplines intact.

## Pre-merge conditions (Fazal-actions; both required)

1. **Wait for CI `orchestrator` job to go green on PR #53.** The 3 new behavioral tests need the pgvector Postgres container that only CI provides; Claude Code's local run was the SHIP-GATE + collect-only + ruff sweep (10p / 8 skipped). The DB-bound tests run on push. Do not merge before that job is green.

2. **Run the canary locally before merging.** CI cannot run it (no `ANTHROPIC_API_KEY`). Command (one Haiku turn, sub-cent):
   ```bash
   RUN_INTEGRATION_TESTS=1 \
     ANTHROPIC_API_KEY=<key> \
     DATABASE_URL=<dsn> \
     pytest apps/team-orchestrator/tests/orchestrator/test_owner_inputs_canary_real_anthropic.py -v
   ```
   Expect: 1 passed. The test asserts a real `msg_*` response id from Anthropic plus a valid `OwnerInputClassification` row in the DB with no raw body leak.

## Post-merge (NOT in this PR)

The flag-flip (`OWNER_INPUTS_EXTRACTION_ENABLED = True`) is a separate Fazal action after this PR merges — own decision per CL-385 launch-gate. Brief hard-rule: not in this PR.

## Status

- `.viabe/queue/VT-OIV/status` → `in-pr`
- Signal `.running/to-cowork/20260524T123000Z-pr-ready-VT-OIV.md` → moved to `.running/processed/`
- Pinging Fazal in chat for merge.

---

## Addendum — pre-merge canary result (recorded 2026-05-24T14:34Z)

**Canary GREEN on retry.** Real-Anthropic + real-Supabase substrate exercised end-to-end.

- First attempt (2026-05-24T14:24Z): ERRORed in fixture setup — `psycopg.OperationalError: database "postgre" does not exist`. DSN typo in `.viabe/secrets/canary.env` (missing trailing `s` on `postgres`). Anthropic spend: $0.00 (never reached the Messages call).
- Fazal fixed DSN; retry pre-merge-check signal issued.
- Retry (2026-05-24T14:32Z signal): `test_owner_inputs_canary_real_anthropic PASSED [100%]` in 28.99s. One Haiku Messages call, sub-cent spend.

Both pre-merge conditions above are now satisfied (CI orchestrator job — green per PR #52 baseline + canary — green per retry). PR #53 ready for Fazal merge in earnest. Status stays `in-pr` until `git log origin/main` shows `c19972f`.
