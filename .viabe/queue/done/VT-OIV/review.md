---
task: VT-OIV
reviewer: cowork
ts: 2026-05-24T16:50:00+05:30
verdict: APPROVED-with-condition
---

# Plan review — VT-OIV

## Verdict: APPROVED with ONE inline addition required during implementation

The plan is sound. Address the one gap below as part of implementation — no need to re-plan; signal `plan-updated` only if your approach changes materially.

## What I'm signing off on

- All four tests are genuinely behavioral (real DB writes/reads, exercise production paths). No source-grep, no transform copies.
- Monkeypatch pattern is correct — `runner.py` reads `OWNER_INPUTS_EXTRACTION_ENABLED` inside the function body at call-time; patching the module attribute works. Same for `classify_message` — patching at `orchestrator.owner_inputs.writer.classify_message` hits the real `run_extraction_for_event` outer try/except.
- Sink 4 (DBOS `workflow_status.inputs`) correctly treated as accepted-per-CL-385 with the inline comment citation — that's exactly the right framing for "verify nothing NEW is leaking."
- The substantive owner-message choice for the resilience test (route to `brain` → `escalated`) is fine — it exercises more of the pipeline than a `STOP` direct-handler would. Approved as-is.
- Canary env-gating mirrors `test_sales_recovery_end_to_end.py`; PR description must call out the manual-run command for me to relay to Fazal pre-merge.
- Migration-comment fix: cite `368387c2-cc5a-8162` is correct.
- Idempotent migration apply check before relying on it — good catch; if the apply path is not idempotent on a pre-existing table, surface via `.running/to-cowork/` as a blocker before committing the comment change. Don't attempt to fix the migration runner in this PR.

## ONE addition required during implementation

**Goal #4 — Composer read-path verification — is not currently in the test plan.** The brief explicitly lists it: "The Composer's `_build_pending_owner_inputs` reads the row when assembling agent context (filters `consumed_at IS NULL`)." Your plan tests extraction → write but not the read-back through the Composer.

Two acceptable ways to close this:

- **(preferred)** In `test_owner_inputs_extraction_writes_structured_row`, after asserting the row was written, call `_build_pending_owner_inputs(tenant_id, conn)` (or whatever signature the existing Composer uses) and assert the returned list contains exactly one entry matching the just-written `intent / segment / occasion / message_sid`. One additional assertion block at the end of the test; same fixtures.
- **(acceptable)** If `test_context_builder_substrate.py` (or similar) already verifies `_build_pending_owner_inputs` against a real DB-seeded row with the `consumed_at IS NULL` filter, cite that test by name and line in the review.md follow-up signal and we treat goal #4 as already-covered. The cite must be specific — not "Composer is tested somewhere."

If you go with the preferred path, no plan revision needed; just add the assertion during implementation. If you go with the cite path, write `.running/to-cowork/<ts>-cite-VT-OIV.md` with the specific test reference so I can verify before you proceed to PR.

## Two soft notes (not blocking, just be aware)

- The resilience test relies on the writer's outer `try/except` being intact. Add a comment in the test linking to `writer.py:240` (the `run_extraction_for_event` function) so a future reader knows what the test is guarding.
- The cross-tenant non-leak assertion in test 3 (`test_owner_inputs_dsr_purge_covers_substrate`) is the kind of thing that's easy to break by reusing the same tenant fixture. Use two distinctly-named tenants (`tenant_alpha`, `tenant_bravo`) so the failure message is readable if it ever fails.

## Proceed to implementation

Status moved to `implementing`. When you've opened the PR, signal `.running/to-cowork/<ts>-pr-ready-VT-OIV.md`. I'll verify the PR and ping Fazal for merge.
