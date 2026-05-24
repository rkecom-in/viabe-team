-- 020_owner_inputs.sql — structured-intent owner-message substrate (VT-146).
--
-- Derived classification of an inbound WhatsApp owner message. The table
-- stores ONLY the structured ``intent / segment / occasion`` extracted by
-- the Component-2 writer (orchestrator/owner_inputs/writer.py) — never the
-- raw message body. Storing raw body would reintroduce the retention
-- surface VT-144 / PR #45 (c7135da) just closed; the brief locks the
-- derived-only shape.
--
-- Provenance is preserved via ``run_id`` (the pipeline run that produced
-- this row) and ``message_sid`` (the originating Twilio MessageSid). With
-- those two foreign-key-like handles you can correlate a classification
-- back to its WhatsApp message without ever persisting the text.
--
-- ``consumed_at`` (CL-decision in this PR) — pending/consumed flag. NULL
-- means the row is still pending — the Composer's
-- ``_build_pending_owner_inputs`` filters to ``consumed_at IS NULL`` so
-- "pending" semantics live in this schema, not in app logic. No code in
-- this PR flips the column; a future row will mark inputs consumed once
-- the agent has acted on them. Pre-seeding the column now avoids a
-- second migration when that lands.
--
-- Retention: lifetime of the tenant relationship (DECISION
-- 368387c2-cc5a-8162 — authoritative; supersedes the earlier
-- 368387c2-cc5a-8180-be0b-d1e64e0366de page). DSR purge wiring is a
-- separate rostered Critical row; this migration does NOT include a
-- purge trigger or TTL.

CREATE TABLE owner_inputs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id),
    run_id        UUID REFERENCES pipeline_runs (id),
    message_sid   TEXT,
    intent        TEXT NOT NULL,
    segment       TEXT,
    occasion      TEXT,
    consumed_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pillar 3: RLS in the same migration that creates the table.
ALTER TABLE owner_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_inputs FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_inputs_select ON owner_inputs FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY owner_inputs_insert ON owner_inputs FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_inputs_update ON owner_inputs FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_inputs_delete ON owner_inputs FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Composer hot-path: ``WHERE tenant_id = ? AND consumed_at IS NULL ORDER
-- BY created_at DESC LIMIT N``. Partial index keeps the pending-row read
-- cheap as the table grows over the tenant relationship's lifetime.
CREATE INDEX owner_inputs_tenant_pending_created
    ON owner_inputs (tenant_id, created_at DESC)
    WHERE consumed_at IS NULL;

-- Provenance lookup — given a Twilio MessageSid, find the row it produced.
CREATE INDEX owner_inputs_tenant_message_sid
    ON owner_inputs (tenant_id, message_sid)
    WHERE message_sid IS NOT NULL;
