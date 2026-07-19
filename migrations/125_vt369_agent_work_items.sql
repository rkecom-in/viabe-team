-- 125_vt369_agent_work_items.sql — VT-369 Gap-5 PR-1: coordinator work-item ledger.
--
-- One row per (tenant, roadmap item) dispatch by the master coordinator: dedupe (the partial unique
-- index keeps ONE open item per roadmap entry), retry accounting, and the audit trail from a Gap-4
-- roadmap item to the agent run that executed it. Tenant-scoped → RLS + FORCE in-migration + swept
-- in dsr_purge._PURGE_ORDER (house discipline, the VT-366 lesson).

CREATE TABLE agent_work_items (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    item_id            TEXT NOT NULL,                 -- the Gap-4 roadmap item_id (stable across versions)
    agent              TEXT NOT NULL CHECK (agent IN
        ('sales_recovery', 'reputation', 'acquisition', 'retention', 'menu_pricing')),
    run_id             UUID NULL REFERENCES pipeline_runs (id) ON DELETE SET NULL,
    status             TEXT NOT NULL DEFAULT 'dispatched' CHECK (status IN
        ('dispatched', 'drafting', 'awaiting_approval', 'approved', 'sending', 'sent',
         'rejected', 'failed', 'cancelled')),
    attempt_count      INT NOT NULL DEFAULT 1,
    last_dispatched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_work_items_tenant_id_uniq UNIQUE (tenant_id, id)
);
-- Dedupe: at most ONE open work item per (tenant, roadmap item).
CREATE UNIQUE INDEX agent_work_items_open ON agent_work_items (tenant_id, item_id)
    WHERE status NOT IN ('sent', 'rejected', 'failed', 'cancelled');

ALTER TABLE agent_work_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_work_items FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_work_items_select ON agent_work_items FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY agent_work_items_insert ON agent_work_items FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_work_items_update ON agent_work_items FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_work_items_delete ON agent_work_items FOR DELETE
    USING (tenant_id = app_current_tenant());
