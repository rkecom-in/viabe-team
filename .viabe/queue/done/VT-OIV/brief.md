---
task: VT-OIV
vt_row: 369387c2-cc5a-8142-8260-daf45fd6ab94
author: cowork
ts: 2026-05-24T15:30:00+05:30
budget_tokens: 250000
budget_minutes: 60
priority: Critical
sprint: Hardening
area: Knowledge Architecture · Privacy
assignee: claudecode
parent: VT-KnowledgeArchitecture (356387c2-cc5a-81088540efd4a78e9188)
---

# Brief — owner_inputs feature verification before flag-flip (VT-OIV)

## Why this task

The owner_inputs feature is the structured-intent substrate that lets the Sales Recovery Agent reason about owner-supplied context. The code is fully merged to `main` (PRs #47/#48) but gated OFF behind `OWNER_INPUTS_EXTRACTION_ENABLED = False` in `runner.py:35`. Per CL-385 standing lock, the feature is decided ON for July launch — **conditional on this verification passing**. That makes this task the launch-gate for owner_inputs in production.

## Step-0 ground-truth — ALREADY DONE (read before planning)

Cowork ran Step-0 on 2026-05-24 14:45 IST. Result: **code matches the authoritative spec.**

| Spec requirement | Code reality | Status |
|---|---|---|
| No raw body column | `migrations/020_owner_inputs.sql:28-38` — table has `intent, segment, occasion, message_sid`; no body column | ✓ |
| Structured intent only | `owner_inputs/writer.py:194` — `write_owner_input` has no `body` parameter; docstring confirms | ✓ |
| Twilio Body-drop preserved | `runner.py:222` body redaction (VT-144) intact | ✓ |
| Lifetime retention, no 90-day timer | Migration 020 comment: "Retention: lifetime of the tenant relationship"; no TTL code | ✓ |
| Body transmitted to Anthropic for classification | `writer.py:158` — `messages=[{"role": "user", "content": body}]`; aligned with CL-385 | ✓ |

**One doc-update note** (in scope as a side fix): migration 020's retention comment cites the SUPERSEDED decision page `368387c2-cc5a-8180` instead of the authoritative `368387c2-cc5a-8162`. The SQL is correct; the comment cites the wrong page. Fix the comment in this PR.

## Goal — verify the feature is safe to flip ON

Produce evidence (passing tests + manual canary run + audit notes) that flipping `OWNER_INPUTS_EXTRACTION_ENABLED = True` in a test environment results in:

1. A representative inbound owner-WhatsApp message gets classified by `classify_message` and yields a valid `OwnerInputClassification` with `intent`, `segment`, `occasion`.
2. `write_owner_input` writes exactly one row to `owner_inputs` with the classification fields, `tenant_id`, `run_id`, `message_sid`, `consumed_at: NULL`, `created_at: now()`.
3. **No raw body** appears anywhere in persistence: `owner_inputs` (verified by schema), `pipeline_runs.trigger_payload`, `pipeline_steps.input_envelope`, `dbos.workflow_status.inputs` (this last one persists ~2.5h per CL-385 — that's accepted, but verify nothing NEW is leaking).
4. The Composer's `_build_pending_owner_inputs` reads the row when assembling agent context (filters `consumed_at IS NULL`).
5. **DSR purge covers owner_inputs** — PR #51 (`378a6d1`) claims 12 tables including owner_inputs; verify by running the purge against a seeded tenant and checking owner_inputs rows are deleted.
6. **Ingress stays resilient on classifier failure** — if `classify_message` raises or times out, the inbound webhook still acks Twilio (no 5xx) and the rest of the pipeline (pre-filter, run-record) continues without the owner_inputs row.
7. Fix the doc-comment in `migrations/020_owner_inputs.sql` to cite `368387c2-cc5a-8162` instead of `368387c2-cc5a-8180`.

## Pass criteria

All seven goal-items above must be demonstrably true via:

- Existing unit tests pass (180 + whatever you add).
- Two new behavioral integration tests (not source-grep; real DB seed + real flag-flip):
  - `test_owner_inputs_extraction_writes_structured_row` — end-to-end with extraction enabled, verifies row shape + no raw body.
  - `test_owner_inputs_dsr_purge_covers_substrate` — seeded tenant with owner_inputs rows + DSR purge → rows deleted.
- One new resilience test:
  - `test_ingress_resilient_on_classifier_failure` — mock classify_message to raise, verify webhook returns 200 and pipeline continues.
- Manual canary (run via the existing real-Anthropic gated test pattern from `test_sales_recovery_end_to_end.py`): one real classification of a representative owner message, screenshot/log the resulting owner_inputs row, confirm no body anywhere.
- Migration comment fixed.

## Out of scope

- Flipping the flag in production (that's Fazal's decision after this PR merges).
- Changes to the writer's classification logic (it works per Step-0).
- Per-tenant attribution recovery-target wiring (separate VT row).
- Approved-templates registry migration (separate VT row).
- Anything that would change the table schema (CL-385 standing lock).

## Reference materials

- Authoritative spec: CL-330 / CL-331 / CL-337 (Notion `Clau_Session_Log`)
- Standing privacy locks: CL-385 (`369387c2-cc5a-8126afa9c2c8ac9319bf`)
- Step-0 audit trail: ALIGNMENT ACK CL-407 (`36a387c2-cc5a-81d4-810e-ddb819eee7b5`)
- DSR-purge implementation: PR #51 (`378a6d1`), `dsr_purge.py:113` _PURGE_ORDER
- Writer code: `apps/team-orchestrator/src/orchestrator/owner_inputs/writer.py`
- Runner gate: `apps/team-orchestrator/src/orchestrator/runner.py:35` + `:262`
- Migration: `migrations/020_owner_inputs.sql`
- VT row: https://www.notion.so/369387c2cc5a81428260daf45fd6ab94

## Hard rules

- **Do NOT** flip `OWNER_INPUTS_EXTRACTION_ENABLED` to True on `main` in this PR. The flag-flip is a separate Fazal-action after merge.
- **Do NOT** add a body column or any raw-body persistence.
- **Do NOT** modify CL-385 standing locks (Anthropic/Twilio/Voyage mandatory consent-gated processing).
- **Do NOT** change the writer's transmission of raw body to Anthropic for classification (that's the authoritative behavior per CL-385).
- PR title: `test(owner_inputs): verification before flag-flip (VT-OIV)`
- Branch: `test/vt-oiv-owner-inputs-verification`

## Open question (Cowork to answer via .running/ if you ask)

The DBOS `workflow_status.inputs` ~2.5h retention of raw Body is accepted per CL-385 + privacy notice. Your verification should confirm nothing NEW is leaking — but don't try to "fix" the DBOS retention (it's intentional, replay-critical). If you find a fourth body-retention sink we didn't know about, that's a CORRECTION to file + escalate; not in scope to fix here.

## Estimated effort

- Reading + understanding existing code: 15 min
- Plan writing: 15 min
- Implementation (3 new tests + comment fix): 30-45 min
- PR opening + CI: 15 min
- Total: ~75 min (within 60 min cap if you skip the canary; canary adds 15 min of human-in-loop)

**Note on canary:** the canary run requires `ANTHROPIC_API_KEY` and a seeded DB. If you can't run it in your environment, write the test as env-gated (same pattern as `test_sales_recovery_end_to_end.py` with `RUN_INTEGRATION_TESTS=1`) and let Fazal run it pre-merge. Document this clearly in the PR description so Fazal knows.

---

**When you're done:** signal `.running/to-cowork/<ts>-pr-ready-VT-OIV.md` with `type: pr-ready`. Cowork verifies; Fazal merges.
