-- 091_vt321_complaint_status.sql — VT-321 #20 complaint-freeze guardrail.
--
-- Fazal's NON-configurable rule #20: "an open complaint freezes ALL selling to
-- that customer — no exceptions." Enforced DETERMINISTICALLY (fail-closed) in
-- the campaign send-cohort path (orchestrator/campaign/execute.py), NOT left to
-- owner approval. This migration ships the STATUS column the exclusion reads.
--
-- STATUS-ONLY by design (CL-390): no complaint content / text is stored at rest.
-- The column carries a 3-state lifecycle only — 'none' (default) → 'open'
-- (a live complaint freezes selling) → 'resolved'. No PII, no body, no subject.
--
-- The inbound complaint-intent classifier that SETS 'open' (on the customer-
-- inbound WABA path, mirroring VT-318's STOP classifier for opt-out) is
-- GATE-LIVE — rostered under VT-321. This row ships the MECHANISM (column +
-- fail-closed exclusion + canary); it is a no-op on real data until that
-- classifier emits, and correct + canaried on synthetic rows.
--
-- Claimed via scripts/migration_id_allocate.py (CL-424). CL-422: dev synthetic-only.

-- ===================== complaint_status column =======================
-- Default 'none' so every existing + future customer is sellable unless a
-- complaint explicitly flips them to 'open'. The fail-closed exclusion treats
-- 'open' as the ONLY freeze trigger ('none'/'resolved'/NULL → sellable), so the
-- NOT NULL DEFAULT means a present-but-unset row never blocks a send.

ALTER TABLE public.customers
    ADD COLUMN IF NOT EXISTS complaint_status TEXT NOT NULL DEFAULT 'none'
    CHECK (complaint_status IN ('none', 'open', 'resolved'));
