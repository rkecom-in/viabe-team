-- 128_vt369_pending_approvals_agent.sql — VT-369 Gap-5 PR-1: pending_approvals agent extension +
-- per-tenant queue serialization.
--
-- (1) New approval_type values for the agent surface (agent_customer_send now; autonomy_upgrade +
--     l3_presend_notice reserved for PR-3 — added to the CHECK now so PR-3 needs no migration).
-- (2) draft_batch_id links an agent approval to its batch. ON DELETE SET NULL is safe because the
--     row carries NO customer PII (batch-id + counts only — the binding no-PII-in-approvals rule).
-- (3) THE SERIALIZATION (Cowork F5): at most ONE open approval per tenant — closes the
--     wrong-surface-approval path (two open rows + one owner "yes"). Pre-step: resolve any stale
--     open rows (the 30-min timeout sweep normally clears these; this is the structural backstop
--     for the index build).

-- Pre-step: time out any stale open rows so the unique index can build.
UPDATE pending_approvals
   SET resolved_at = now(), decision = 'timeout'
 WHERE resolved_at IS NULL
   AND tenant_id IN (
       SELECT tenant_id FROM pending_approvals
        WHERE resolved_at IS NULL
        GROUP BY tenant_id HAVING count(*) > 1
   )
   AND id NOT IN (
       SELECT DISTINCT ON (tenant_id) id FROM pending_approvals
        WHERE resolved_at IS NULL
        ORDER BY tenant_id, created_at DESC
   );

ALTER TABLE pending_approvals DROP CONSTRAINT pending_approvals_approval_type_check;
ALTER TABLE pending_approvals ADD CONSTRAINT pending_approvals_approval_type_check
    CHECK (approval_type IN (
        'campaign_send', 'cohort_size_exceeded', 'sensitive_data_access', 'other',
        'agent_customer_send', 'autonomy_upgrade', 'l3_presend_notice'));

ALTER TABLE pending_approvals ADD COLUMN draft_batch_id UUID NULL;
-- ON DELETE SET NULL **(draft_batch_id)** — the column list is LOAD-BEARING (PG15+): without it
-- Postgres nulls ALL referencing columns including the NOT NULL tenant_id, which makes the DSR
-- purge's DELETE FROM agent_draft_batches raise NotNullViolation and roll back the whole purge —
-- right-to-erasure permanently broken for any tenant with an agent approval row (adversarial-verify
-- Probe 6, reproduced empirically).
ALTER TABLE pending_approvals ADD CONSTRAINT pending_approvals_draft_batch_fk
    FOREIGN KEY (tenant_id, draft_batch_id)
    REFERENCES agent_draft_batches (tenant_id, id) ON DELETE SET NULL (draft_batch_id);

CREATE UNIQUE INDEX pending_approvals_one_open_per_tenant
    ON pending_approvals (tenant_id) WHERE resolved_at IS NULL;
