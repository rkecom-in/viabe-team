-- 126_vt369_agent_drafts.sql — VT-369 Gap-5 PR-1: agent draft batches + per-customer drafts.
--
-- A specialist agent's drafted customer sends, batched per (work item, generation cycle). The batch
-- carries the Pillar-7 state (awaiting_approval → approved/rejected/…); each draft carries ONLY
-- template_name + params (CL-390: params are the rendered variables — customer display name is a
-- param, encrypted phone lives in customers/phone_token_resolutions, NEVER here in plaintext beyond
-- the param fields the owner reviews; pending_approvals carries batch_id + counts ONLY).
-- Tenant-scoped → RLS + FORCE + dsr_purge._PURGE_ORDER.

CREATE TABLE agent_draft_batches (
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    work_item_id  UUID NOT NULL,
    agent         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'drafting' CHECK (status IN
        ('drafting', 'awaiting_approval', 'approved', 'auto_send_pending', 'sending',
         'sent', 'edit_requested', 'rejected', 'cancelled', 'halted')),
    edit_cycles   INT NOT NULL DEFAULT 0,
    owner_feedback TEXT NULL,             -- the owner's needs_changes reply body (RLS-read only)
    send_not_before TIMESTAMPTZ NULL,     -- L3 notice window (PR-3; column reserved now)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT agent_draft_batches_work_item_fk
        FOREIGN KEY (tenant_id, work_item_id)
        REFERENCES agent_work_items (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE agent_drafts (
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    batch_id      UUID NOT NULL,
    customer_id   UUID NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    template_name TEXT NOT NULL,          -- registry name; resolution + category check at SEND time
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'drafted' CHECK (status IN
        ('drafted', 'sent', 'skipped', 'halted')),
    skip_reason   TEXT NULL,              -- skipped_opt_out / skipped_consent / skipped_caps / ...
    message_sid   TEXT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT agent_drafts_batch_fk
        FOREIGN KEY (tenant_id, batch_id)
        REFERENCES agent_draft_batches (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX agent_drafts_batch ON agent_drafts (tenant_id, batch_id);

ALTER TABLE agent_draft_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_draft_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_draft_batches_select ON agent_draft_batches FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY agent_draft_batches_insert ON agent_draft_batches FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_draft_batches_update ON agent_draft_batches FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_draft_batches_delete ON agent_draft_batches FOR DELETE
    USING (tenant_id = app_current_tenant());

ALTER TABLE agent_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_drafts FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_drafts_select ON agent_drafts FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY agent_drafts_insert ON agent_drafts FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_drafts_update ON agent_drafts FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_drafts_delete ON agent_drafts FOR DELETE
    USING (tenant_id = app_current_tenant());
