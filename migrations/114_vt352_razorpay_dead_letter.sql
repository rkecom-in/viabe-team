-- 114_vt352_razorpay_dead_letter.sql — VT-352: durable Razorpay webhook dead-letter (Option B).
--
-- VT-330 record-and-drop commits a parse-error event into razorpay_webhook_events with a
-- {"_status":"dropped_parse_error"} marker for MANUAL reconciliation. This sibling table is the
-- durable, queryable QUEUE of those drops for PROGRAMMATIC replay before the LIVE money path:
-- the ingress writes a dead-letter row on every drop; a re-POST of the corrected event (same
-- event_id) re-processes through the ingress (F1) and marks the row 'replayed'. SOFT GATE — this
-- MUST exist before TEAM_RAZORPAY_LIVE=1.
--
-- Pure-additive (new table). Service-role-only (deny-all RLS) like razorpay_webhook_events — the
-- ingress endpoint is the only writer. PII-FREE (CL-390): event_payload stores ONLY the redacted
-- routing fields (subscription_id, amount_paise), NOT the raw event — the raw is already kept ONCE
-- in razorpay_webhook_events.payload.raw (VT-330's drop marker) for manual reconciliation, so this
-- queue does not double-store customer PII (the raw Razorpay payment entity carries email/contact/
-- card). This table is the durable QUEUE + observability of drops; the replay re-feeds through the
-- ingress (which re-reads the corrected event), keyed on event_id.
CREATE TABLE IF NOT EXISTS razorpay_webhook_dead_letter (
    event_id      TEXT PRIMARY KEY,                       -- Razorpay event.id (1 dead-letter per event)
    event_type    TEXT,
    event_payload JSONB        NOT NULL,                   -- redacted routing only (PII-free; raw is in webhook_events)
    error_reason  TEXT         NOT NULL,                   -- why it dropped (e.g. 'non_int_charged_amount')
    retry_count   INT          NOT NULL DEFAULT 0,         -- bumped each replay attempt
    -- F5 (Cowork fold-in): only the two states the code actually writes — the ingress sets a row
    -- 'pending' on drop and flips it 'replayed' on a successful replay. (A failed replay raises +
    -- rolls back, leaving the row 'pending' — re-replayable — so there is no 'failed' state to
    -- promise. Don't encode an unwritten status the schema can't honour.)
    status        TEXT         NOT NULL DEFAULT 'pending', -- pending | replayed
    first_seen    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_retry    TIMESTAMPTZ,
    CONSTRAINT razorpay_dead_letter_status_chk CHECK (status IN ('pending', 'replayed'))
);

-- Observability: find the still-stuck events (what an operator/sweep must replay).
CREATE INDEX IF NOT EXISTS razorpay_dead_letter_pending_idx
    ON razorpay_webhook_dead_letter (first_seen) WHERE status = 'pending';

ALTER TABLE razorpay_webhook_dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE razorpay_webhook_dead_letter FORCE ROW LEVEL SECURITY;
-- F6 (Cowork fold-in): an EXPLICIT deny-all policy, for parity with mig 004 (razorpay_webhook_
-- events). No policy already denies all under FORCE RLS, but the explicit policy documents intent +
-- matches the sibling table — only the Supabase service role / superuser (the ingress) touches it.
DROP POLICY IF EXISTS razorpay_dead_letter_no_tenant_access ON razorpay_webhook_dead_letter;
CREATE POLICY razorpay_dead_letter_no_tenant_access ON razorpay_webhook_dead_letter
    FOR ALL USING (false) WITH CHECK (false);
