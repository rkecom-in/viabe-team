-- 143_vt467_business_autonomy.sql — VT-467 business-impact rails: the per-(tenant, action_class)
-- business-autonomy threshold + tier state, and the pending_approvals approval_type extension.
--
-- VT-467 extends the VT-460 rail harness to CONSEQUENTIAL business-impact actions (spend money /
-- external commitment / config-integration change — customer SEND is already gated by VT-460). The
-- manager/specialist emits an INTENT; a DETERMINISTIC gate decides autonomous-vs-owner-approval from
-- {action class, magnitude/threshold, tenant autonomy tier}. This table is the per-tenant tier +
-- threshold the gate reads — the business-impact analogue of tenant_agent_autonomy (mig 129), kept a
-- SEPARATE table because the axis is different: tenant_agent_autonomy is per-(tenant, AGENT) and
-- governs CUSTOMER-SEND L2/L3 trust; this is per-(tenant, ACTION-CLASS) and governs SPEND/COMMIT/
-- CONFIG magnitude thresholds. Conflating them would overload one row with two unrelated decisions.
--
-- DECAYING-HITL (design §7, REUSES the VTR model's SHAPE, not its row): the approval requirement
-- LOOSENS as the owner grants the manager more autonomy. Deterministic, never the brain's vibe:
--   tier 'always_approve' (the DEFAULT — a MISSING row IS this, fail-closed) → EVERY action of the
--     class needs owner approval, regardless of magnitude.
--   tier 'threshold'    → autonomous BELOW auto_approve_below_minor (the magnitude unit per class);
--     at/above it → owner approval. The owner grants this + sets the threshold (the autonomy grant).
--   tier 'autonomous'   → autonomous up to a hard ceiling; only the extreme (above ceiling, or a
--     'frozen' kill) escalates. Earned/granted; never the default.
-- The owner LOOSENS by raising the tier / the threshold (the grant); a regression/kill TIGHTENS
-- (frozen=true → back to always-approve behaviour). Same monotonic-trust intuition as L2→L3, applied
-- to a magnitude axis instead of a send-count streak.
--
-- magnitude unit by class (the gate documents this; the column is class-agnostic INTEGER "minor
-- units"): SPEND = paise (₹1 = 100), COMMITMENT = an owner-defined commitment-weight, CONFIG = an
-- owner-defined change-weight. Stored class-agnostic so one table covers all three.
--
-- FAIL-CLOSED is structural: no row → always_approve (the gate's default), so a tenant with no grant
-- gets owner-approval on EVERY business-impact action. A grant is an explicit owner act, never a default.
--
-- Tenant-scoped → RLS + FORCE + dsr_purge._PURGE_ORDER (house discipline, mirrors mig 129).

CREATE TABLE tenant_business_autonomy (
    tenant_id                 UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    -- The business-impact action class this row governs (customer SEND is NOT here — VT-460 owns it).
    action_class              TEXT NOT NULL
                              CHECK (action_class IN ('spend', 'commitment', 'config')),
    -- The decaying-HITL tier. Default = the fail-closed floor (a MISSING row is read as this too).
    tier                      TEXT NOT NULL DEFAULT 'always_approve'
                              CHECK (tier IN ('always_approve', 'threshold', 'autonomous')),
    -- The magnitude threshold (minor units, class-agnostic): tier='threshold' is autonomous strictly
    -- BELOW this; at/above → owner approval. NULL with tier='threshold' means "0 threshold" = always
    -- approve (fail-closed). Ignored by the other tiers.
    auto_approve_below_minor  BIGINT NULL CHECK (auto_approve_below_minor IS NULL OR auto_approve_below_minor >= 0),
    -- The hard ceiling for tier='autonomous': autonomous up to (and including) this, escalate ABOVE
    -- it. NULL = no ceiling (autonomous for any magnitude — only the 'frozen' kill escalates). The
    -- owner sets this when granting full autonomy; it is the "extreme scenario" escalation line (§6).
    autonomous_ceiling_minor  BIGINT NULL CHECK (autonomous_ceiling_minor IS NULL OR autonomous_ceiling_minor >= 0),
    -- The kill switch (owner keyword / VTR override / Ops): true → the gate treats the class as
    -- always_approve regardless of tier (a frozen class never acts autonomously). Mirrors
    -- tenant_agent_autonomy.frozen.
    frozen                    BOOLEAN NOT NULL DEFAULT false,
    -- Provenance of the grant (the owner act that loosened the tier) — an approval row id when the
    -- grant rode an owner approval, else NULL. Audit only; the gate never trusts it for a decision.
    granted_by_approval_id    UUID NULL,
    granted_at                TIMESTAMPTZ NULL,
    last_regression_at        TIMESTAMPTZ NULL,
    last_regression_reason    TEXT NULL,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, action_class)
);

ALTER TABLE tenant_business_autonomy ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_business_autonomy FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_business_autonomy_select ON tenant_business_autonomy FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_autonomy_insert ON tenant_business_autonomy FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_autonomy_update ON tenant_business_autonomy FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_autonomy_delete ON tenant_business_autonomy FOR DELETE
    USING (tenant_id = app_current_tenant());

-- The pending_approvals approval_type extension: the VT-467 business-impact action ask routes
-- through the EXISTING owner-approval machinery (arm_pause_request / request_owner_approval_node /
-- route_after_approval — the same Pillar-7 path agent_customer_send uses), so it needs its own
-- approval_type. The ApprovalType Literal in agent/tools/request_owner_approval.py is the source of
-- truth (CL-428); this keeps the DB CHECK in exact sync, same PR.
ALTER TABLE pending_approvals DROP CONSTRAINT pending_approvals_approval_type_check;
ALTER TABLE pending_approvals ADD CONSTRAINT pending_approvals_approval_type_check
    CHECK (approval_type IN (
        'campaign_send', 'cohort_size_exceeded', 'sensitive_data_access', 'other',
        'agent_customer_send', 'autonomy_upgrade', 'l3_presend_notice',
        'business_impact_action'));
