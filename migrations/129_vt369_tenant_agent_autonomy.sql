-- 129_vt369_tenant_agent_autonomy.sql — VT-369 Gap-5 PR-2: the per-(tenant, agent) autonomy state.
--
-- L2 = owner approves EACH send (the default — a missing row IS L2). L3 = earned auto-send
-- (PR-3): proposed at a 20-clean-approval streak, granted ONLY by explicit owner opt-in (the
-- autonomy_upgrade approval row = the consent evidence, C3), revoked on ANY regression event.
-- ``frozen`` is the kill switch (owner keyword / VTR override / Ops): NO dispatch while set, and
-- every freeze/revoke atomically cancels in-flight batches (the binding rule — a kill switch never
-- leaves armed batches ticking). PR-2 ships the substrate + hooks with ZERO auto-send behavior.
-- Tenant-scoped → RLS + FORCE + dsr_purge._PURGE_ORDER (house discipline).

CREATE TABLE tenant_agent_autonomy (
    tenant_id                    UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    agent                        TEXT NOT NULL,
    level                        TEXT NOT NULL DEFAULT 'L2' CHECK (level IN ('L2', 'L3')),
    clean_approval_streak        INT NOT NULL DEFAULT 0,
    lifetime_approvals           INT NOT NULL DEFAULT 0,
    lifetime_rejections          INT NOT NULL DEFAULT 0,
    consecutive_silent_l3_notices INT NOT NULL DEFAULT 0,  -- the owner-disengagement counter (PR-3)
    last_regression_at           TIMESTAMPTZ NULL,
    last_regression_kind         TEXT NULL,
    last_promotion_proposed_at   TIMESTAMPTZ NULL,
    l3_granted_at                TIMESTAMPTZ NULL,
    l3_grant_approval_id         UUID NULL,   -- the autonomy_upgrade approval row = consent evidence (C3)
    l3_revoked_at                TIMESTAMPTZ NULL,
    revoke_reason                TEXT NULL,
    frozen                       BOOLEAN NOT NULL DEFAULT false,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent)
);

ALTER TABLE tenant_agent_autonomy ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_agent_autonomy FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_agent_autonomy_select ON tenant_agent_autonomy FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_agent_autonomy_insert ON tenant_agent_autonomy FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_agent_autonomy_update ON tenant_agent_autonomy FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_agent_autonomy_delete ON tenant_agent_autonomy FOR DELETE
    USING (tenant_id = app_current_tenant());
