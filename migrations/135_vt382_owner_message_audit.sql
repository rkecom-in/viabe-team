-- 135_vt382_owner_message_audit.sql — VT-382 (CL-437 ruling 3): the tenant-scoped audit
-- surface that makes the EXACT owner-facing sent message RECONSTRUCTIBLE.
--
-- STEP-0 proved NO surface persists the send-resolved variable values today: owner_inputs
-- is derived-only BY DESIGN (mig 020 / VT-144), wa_conversations is phone-token markers
-- only (mig 070), send_idempotency_keys / campaign_messages carry SID + status only
-- (mig 049). CL-437.3 retains the reconstruction substrate ONLY here — under normal
-- tenant retention + DSR (dsr_purge._PURGE_ORDER) — while the outbox copy
-- (agent_drafts.params / agent_draft_batches.owner_feedback, mig 126) is redacted on
-- terminal completion (metadata + hashes kept).
--
-- One row per SENT agent draft, captured at the agent_drafts -> 'sent' flip in the SAME
-- transaction as the status flip + params redaction (orchestrator/agents/
-- outbox_redaction.py — atomic: no window where the outbox copy is gone but the audit
-- row absent).
--
-- What rendered_text captures (CL-437 + Fazal 'accept' 2026-06-12 — the RULED
-- interpretation): the template REF + the ordered send-resolved variable VALUES
-- (str()-coerced params in the registry's positional variable order —
-- customer_send.agent_send_draft / send_whatsapp_template._build_content_variables).
-- It is NOT a literal Meta-rendered body snapshot: the fixed approved body lives at
-- Meta/Twilio, not in our store. The EXACT owner-facing text is RECONSTRUCTIBLE by
-- folding these ordered values into the registry's pinned approved body for that
-- template+language (body_sha256-pinned in config/twilio_templates.yaml, landing with
-- the F1 SIDs) — that pin is what makes the reconstruction exact + drift-detectable.
-- message_sid (the resolved Twilio SID) pins which approved body was sent.
-- skipped/halted drafts capture NOTHING (no send happened).
--
-- RLS posture MATCHES the sibling agent tables (mig 126 agent_drafts): ENABLE + FORCE +
-- the four per-command tenant policies via app_current_tenant(). app_role reaches the
-- table through the mig-015 ALTER DEFAULT PRIVILEGES (no explicit GRANT needed — the
-- mig-095 convention).
--
-- ZERO app_vtr_role grants — BY DESIGN. This table holds raw owner-facing message text
-- (rendered params include third-party customer display names); the VTR world is
-- keys-only (VT-281/VT-376). If a VTR door onto this table is ever wanted, that is a
-- FRESH PII-audit decision (its own VT row + ruling), never a grant copied from a
-- sibling table.

CREATE TABLE owner_message_audit (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    draft_id      UUID NOT NULL,   -- agent_drafts.id (no FK: the audit row must survive outbox-row deletion)
    batch_id      UUID NOT NULL,   -- agent_draft_batches.id (same — linkage by value, not constraint)
    customer_id   UUID NULL,       -- recipient at send time (nullable: audit outlives customer hard-deletes)
    template_name TEXT NULL,
    rendered_text TEXT NOT NULL,   -- template ref + ordered send-resolved variable values
                                   -- (exact text RECONSTRUCTIBLE via the registry's pinned
                                   -- approved body; NOT a literal Meta-rendered snapshot)
    message_sid   TEXT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one audit row per sent draft: the capture is idempotent under retry/replay
-- (the helper inserts via WHERE NOT EXISTS; this index makes the invariant structural).
CREATE UNIQUE INDEX owner_message_audit_draft ON owner_message_audit (tenant_id, draft_id);
CREATE INDEX owner_message_audit_sent ON owner_message_audit (tenant_id, sent_at);

ALTER TABLE owner_message_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_message_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_message_audit_select ON owner_message_audit FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY owner_message_audit_insert ON owner_message_audit FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_message_audit_update ON owner_message_audit FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY owner_message_audit_delete ON owner_message_audit FOR DELETE
    USING (tenant_id = app_current_tenant());
