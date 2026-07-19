-- 170_vt610_force_l3_provenance.sql — VT-610 (loop program Package 7): the VTR force_l3 override.
--
-- Context: L2→L3 autonomy is EARNED (a 20-clean-approval streak) + requires explicit OWNER opt-in
-- (grant_l3's approval_id consent evidence, migration 129). A VTR needs a per-capability OVERRIDE
-- for the rare case an owner can't/won't complete that flow but a verified VTR has independently
-- confirmed the agent is trustworthy — bypassing ONLY the earning threshold + owner opt-in, never
-- any other rail (policy/consent/opt-out/caps/ownership/activation/effect gates are unconditional,
-- read from entirely separate tables/functions, and never inspect WHY a tenant is at L3).
--
-- NEW columns distinguish FORCED provenance from EARNED provenance (the Ops UI reads both,
-- CL-390 — no revoke_reason equivalent free-text field for force; the VTR identity + timestamp
-- ARE the audit trail, same as l3_grant_approval_id is for an earned grant):
--   l3_force_granted_at      — set by autonomy.force_l3, NULL for an earned-only grant.
--   l3_force_granted_by_vtr  — the VERIFIED operator id (never a body-trusted value — the same
--                              require_vtr_action-returned id every other VTR action attributes to).
-- l3_granted_at / l3_grant_approval_id stay NULL on a pure force — "earned" and "forced" are
-- independent, both-nullable markers; a tenant can show as forced-but-never-earned, which is the
-- expected/only state for a force_l3 that never also went through the earn flow.

ALTER TABLE tenant_agent_autonomy
    ADD COLUMN l3_force_granted_at     TIMESTAMPTZ NULL,
    ADD COLUMN l3_force_granted_by_vtr TEXT NULL;

-- Body: mig-134's vtr_agent_autonomy verbatim (assignment-scoped predicate unchanged) + the two
-- new provenance columns APPENDED at the end, so the Ops UI can distinguish earned
-- (l3_granted_at set) from forced (l3_force_granted_at set) without a second query. Postgres
-- forbids CREATE OR REPLACE VIEW from renaming/repositioning an EXISTING output column (it
-- errors "cannot change name of view column ... to ..." if a new column is inserted before one
-- of the old ones) — the new columns MUST go after ``updated_at``, not before it.
CREATE OR REPLACE VIEW vtr_agent_autonomy AS
    SELECT a.tenant_id, t.business_name AS tenant_name, a.agent, a.level,
           a.clean_approval_streak, a.lifetime_approvals, a.lifetime_rejections, a.frozen,
           a.last_regression_at, a.last_regression_kind,
           a.l3_granted_at, a.l3_revoked_at, a.updated_at,
           a.l3_force_granted_at, a.l3_force_granted_by_vtr
    FROM tenant_agent_autonomy a JOIN tenants t ON t.id = a.tenant_id
    WHERE current_user = 'app_vtr_admin_role'
       OR a.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);
