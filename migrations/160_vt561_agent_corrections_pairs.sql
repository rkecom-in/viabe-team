-- 160_vt561_agent_corrections_pairs.sql — VT-561: make the correction store TRAINABLE PAIRS.
--
-- VT-531 (mig 154) shipped agent_corrections as a LABEL store: the owner's PII-redacted prose
-- ("make it shorter, drop the discount") survived, but the ARTIFACT the label describes did not.
-- Three confirmed audit findings against that store:
--
--   (a) On reject/edit, apply_agent_decision records the correction PROSE, then redact_batch_close
--       sha256-DESTROYS the agent_drafts params in the SAME transaction. The label lives; the thing
--       it labels ("what the agent actually proposed") is gone. A label with no example is not a
--       trainable pair.
--   (b) Approve-as-is only bumps the clean_approval_streak (autonomy.record_approval_outcome) — no
--       labeled POSITIVE example is ever written. The dataset accumulates ONLY negatives.
--   (c) apply_agent_decision had no run_id → rows land run_id=NULL. The decision→action→outcome
--       chain is not joinable.
--
-- THIS MIGRATION adds the columns that make each row a self-contained (proposal → verdict →
-- correction) example, and widens the kind CHECK so approve-as-is (b) has a kind:
--
--   proposal_snapshot  — PII-REDACTED snapshot of what the agent PROPOSED (per-draft template +
--                        params), captured BEFORE redact_batch_close destroys the drafts.
--   corrected_snapshot — the corrected/edited artifact when a later phase can supply it.
--   outcome            — Phase-2 back-annotation of how the proposal fared (recovered/lost/…).
--
-- SNAPSHOT PII POSTURE (binding, same as correction_text): the snapshots are redacted through the
-- SAME pii_redactor that owns correction_text — NOT sha256. The learning SUBSTANCE (template +
-- redacted params) survives while customer PII is stripped. sha256 (outbox_redaction) is a DESTROY;
-- this store is a KEEP-the-substance. The two must never be confused.
--
-- AHEAD OF ITS CONSUMER: outcome (and corrected_snapshot for the reject/edit path today) ship
-- DEFAULT NULL with no writer/reader yet — the codebase's standing "add the column ahead of its
-- consumer" practice, exactly as agent_corrections' retrieval-gate columns did (mig 154).
--
-- DSR: agent_corrections is already in dsr_purge (row deletion, VT-518) — these columns ride the
-- existing per-row erasure; no new right-to-erasure wiring needed.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + DROP CONSTRAINT IF EXISTS before the re-add, so a re-run
-- (or a fixture that re-applies the file) is a no-op.

ALTER TABLE agent_corrections
    ADD COLUMN IF NOT EXISTS proposal_snapshot  JSONB NULL,   -- PII-redacted: template + params per draft
    ADD COLUMN IF NOT EXISTS corrected_snapshot JSONB NULL,   -- PII-redacted corrected artifact (when available)
    ADD COLUMN IF NOT EXISTS outcome            TEXT  NULL;   -- Phase-2 back-annotation; no consumer yet

-- Widen the correction_kind CHECK to admit the positive 'approve' lesson (drop + re-add; the inline
-- CREATE-TABLE column CHECK is auto-named <table>_<column>_check — migration 158's status-CHECK
-- idiom). The new set is a SUPERSET of the old, so every existing row still satisfies it — no data
-- rewrite.
ALTER TABLE agent_corrections DROP CONSTRAINT IF EXISTS agent_corrections_correction_kind_check;
ALTER TABLE agent_corrections ADD CONSTRAINT agent_corrections_correction_kind_check
    CHECK (correction_kind IN ('edit', 'reject', 'approve'));

COMMENT ON COLUMN agent_corrections.proposal_snapshot IS
    'VT-561: PII-redacted (pii_redactor, NOT sha256) snapshot of what the agent proposed — per-draft template + params — captured BEFORE redact_batch_close destroys the drafts. The trainable example the correction labels.';
COMMENT ON COLUMN agent_corrections.corrected_snapshot IS
    'VT-561: PII-redacted corrected/edited artifact when a phase can supply it; NULL until a producer exists (ahead-of-consumer).';
COMMENT ON COLUMN agent_corrections.outcome IS
    'VT-561: Phase-2 back-annotation of how the proposal fared (recovered/lost/…); DEFAULT NULL, no consumer yet.';
