-- 154_vt531_agent_corrections.sql — VT-531 (C3): the reviewer-correction store.
--
-- THE GAP: when an owner edits or rejects an agent draft, the raw ``owner_feedback`` is stored on
-- ``agent_draft_batches`` and then — the moment the batch goes terminal — ``redact_batch_close``
-- (outbox_redaction) overwrites it with a ``redacted:sha256:<hex>`` marker (CONTENT DESTROYED).
-- So the single most valuable learning signal ("what the owner actually said to fix it") was gone;
-- only an aggregate regression counter survived (autonomy.tenant_agent_autonomy).
--
-- THIS TABLE captures every correction the moment it lands, keyed by (tenant, batch), storing the
-- correction text **PII-REDACTED (pii_redactor) — NOT sha256'd** — so the SUBSTANCE survives
-- ("make it shorter, drop the discount") while any customer PII is stripped. Append-only, one row
-- per correction event (the tm_audit_log shape), correlated by run_id/batch_id — no mutable
-- outcome row, no hard FK to the audit spine.
--
-- CAPTURE-NOW, RETRIEVE-LATER: retrieval gating (tenant scope, authority, expiry, contradiction
-- resolution) is Phase-2. The gate columns ship NOW, DEFAULT-CLOSED (retrieval_eligible=false), so
-- Phase-2 activation needs no ALTER + backfill — the codebase's standing "add the column ahead of
-- its consumer" practice.
--
-- Tenant-scoped → RLS + FORCE + operator SELECT + dsr_purge in the SAME PR (VT-518): a redacted
-- correction history is STILL the subject's data → erased on right-to-erasure.

CREATE TABLE agent_corrections (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    run_id             UUID NULL,                          -- soft correlation (no FK)
    batch_id           UUID NULL,                          -- soft ref → agent_draft_batches (no FK)
    agent              TEXT NULL,                           -- owning lane (sales_recovery / …)
    correction_kind    TEXT NOT NULL CHECK (correction_kind IN ('edit', 'reject')),  -- reuse RegressionKind
    decision_verb      TEXT NOT NULL,                       -- needs_changes | rejected | timeout | defer
    correction_text    TEXT NULL,                           -- PII-REDACTED (pii_redactor), NOT sha256 — the learning substrate
    -- Retrieval-gate placeholders — DEFAULT-CLOSED, unused until Phase-2 activation (no ALTER later).
    retrieval_eligible BOOLEAN NOT NULL DEFAULT false,
    authority          TEXT NULL,                           -- who the correction came from (owner / vtr / system)
    expires_at         TIMESTAMPTZ NULL,                    -- optional recency window for retrieval
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX agent_corrections_tenant_created ON agent_corrections (tenant_id, created_at DESC);
CREATE INDEX agent_corrections_batch ON agent_corrections (batch_id) WHERE batch_id IS NOT NULL;
-- Phase-2 retrieval will scan eligible rows; the partial index is ready for it (default-closed today).
CREATE INDEX agent_corrections_retrievable
    ON agent_corrections (tenant_id, agent) WHERE retrieval_eligible;

ALTER TABLE agent_corrections ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_corrections FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_corrections_select ON agent_corrections FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY agent_corrections_insert ON agent_corrections FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_corrections_update ON agent_corrections FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_corrections_delete ON agent_corrections FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE POLICY agent_corrections_operator_select ON agent_corrections
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
