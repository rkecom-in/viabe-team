-- 144_vt474_business_policy.sql — VT-474 A2: the per-tenant, machine-enforceable BUSINESS POLICY.
--
-- A2 ruling (design §8): "within policy" = a DETERMINISTIC bound-check (a rail/guard), NOT the
-- brain's self-judgment. The onboarding-granted policy = machine-enforceable BOUNDS the manager +
-- specialists act inside. The brain emits an action INTENT; assert_within_policy() decides
-- IN_POLICY / OUT_OF_POLICY deterministically and the brain CANNOT reason itself out of it.
--
-- WHY A SEPARATE, SINGLE-ROW-PER-TENANT TABLE (not a column on tenant_business_autonomy):
--   tenant_business_autonomy (mig 143) is per-(tenant, ACTION-CLASS) and governs the
--   SPEND/COMMIT/CONFIG *magnitude-threshold* decay (autonomous-vs-approval by amount). The POLICY
--   is a different grain: it is TENANT-WIDE (ONE row per tenant) and bounds WHICH segments the team
--   may touch, the frequency caps, the spend ceiling, and the allowed action-types — orthogonal to
--   the per-class magnitude tier. Hanging a tenant-wide policy off a per-class row would either
--   duplicate it across classes or pick an arbitrary "owning" class. A single-row-per-tenant table
--   (the business_profile_draft / tenant_agent_autonomy shape) is the correct home. The two compose:
--   the policy is the OUTER bound (is this action even ALLOWED + within caps/ceiling); the
--   per-class autonomy tier is the INNER decay (given it is allowed, autonomous or owner-approval).
--
-- FAIL-CLOSED (structural, the A2 hardening): a MISSING row → the MOST-RESTRICTIVE policy. With no
-- explicit owner grant the guard treats EVERY action class as not-allowed and every cap as 0 — so a
-- tenant with no policy can take NO autonomous business action; it routes to owner approval. A policy
-- is an explicit owner act (granted at onboarding), never a default. The application guard
-- (agents/business_policy.py) constructs the most-restrictive policy for a NULL row; this table only
-- stores the EXPLICIT grant.
--
-- The policy JSONB shape the guard reads (documented here; the column is schemaless so the policy can
-- grow without a migration per field — the guard owns validation, fail-closed on a malformed field):
--   {
--     "allowed_action_types": ["customer_send", "spend", "commitment", "config"],  -- absent/[] = none
--     "allowed_segments":     ["lapsed", "vip", "all"],          -- absent/[] = no segment allowed
--     "frequency_caps":       {"customer_send_per_day": 200, ...},-- absent/missing key = 0 (deny)
--     "spend_ceiling_minor":  50000                               -- paise; absent = 0 (deny any spend)
--   }
-- Every absent/empty field is the DENY value (fail-closed), so a partial policy never silently widens.
--
-- Tenant-scoped → RLS + FORCE + dsr_purge._PURGE_ORDER (house discipline, mirrors mig 129 / 143).

CREATE TABLE tenant_business_policy (
    tenant_id    UUID PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    -- The machine-enforceable policy bounds (shape documented above). Schemaless so the policy can
    -- grow per field without a migration; the guard validates + fails closed on a malformed field.
    -- DEFAULT '{}' is itself the most-restrictive policy (every field absent ⇒ deny) — so even an
    -- explicit empty grant is fail-closed, not a wildcard.
    policy       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Provenance of the grant (the owner act that set the policy) — an approval/onboarding row id
    -- when it rode an owner approval, else NULL. Audit only; the guard never trusts it for a decision.
    granted_by   UUID NULL,
    granted_at   TIMESTAMPTZ NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tenant_business_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_business_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_business_policy_select ON tenant_business_policy FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_policy_insert ON tenant_business_policy FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_policy_update ON tenant_business_policy FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY tenant_business_policy_delete ON tenant_business_policy FOR DELETE
    USING (tenant_id = app_current_tenant());
