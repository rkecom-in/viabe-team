-- 072_operator_assignments.sql — VT-290 (VT-189 Ops Console V2) VTR↔business scoping.
--
-- The Ops Console has two operator roles: VTR (sees only ASSIGNED businesses) and
-- VTAdmin (sees all + controls assignment). This table is the assignment substrate every
-- Ops sub-row (VT-291..298) scopes by. The TABLE is foundational and lands in VT-290; the
-- assignment-management UI is VT-295. Cowork-approved 2026-06-02 (VT-290 plan, answer #3).
--
-- Enforcement model (consistent with the VT-188/VT-228 operator substrate): workspace-
-- scoped, **deny-all FORCE RLS** — only the service-role connection (team-web
-- serverSecretClient / orchestrator pool, both RLS-bypassing) touches it. VTR scoping is
-- applied APP-SIDE in team-web data-access (queries filtered to the operator's active
-- assigned tenant set), fail-CLOSED (no assignments → sees nothing). The migration runner
-- has no Supabase auth/JWT context, so JWT-claim RLS can't live here (VT-228 precedent).
--
-- No FK to auth.users (auth schema absent in the CI migrations runner — VT-228). operator_id
-- is a bare UUID validated app-side. Soft-delete via unassigned_at (audit trail, re-assignable).
-- CL-422: synthetic on dev until VT-231. Migration 072 via the allocator (CL-424).

CREATE TABLE IF NOT EXISTS public.operator_assignments (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id    UUID NOT NULL,                 -- the VTR (operator_allowlist.user_id)
    tenant_id      UUID NOT NULL,                 -- the assigned business
    assigned_by    UUID NULL,                     -- the VTAdmin who assigned (NULL = seed)
    assigned_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    unassigned_at  TIMESTAMPTZ NULL,              -- non-NULL = revoked (kept for audit)
    notes          TEXT NULL
);

-- One ACTIVE assignment per (operator, tenant); re-assignment after unassign is allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_assignments_active
    ON public.operator_assignments (operator_id, tenant_id)
    WHERE unassigned_at IS NULL;

-- Hot scoping lookups: a VTR's active tenants; a tenant's active operators.
CREATE INDEX IF NOT EXISTS idx_operator_assignments_operator
    ON public.operator_assignments (operator_id) WHERE unassigned_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_operator_assignments_tenant
    ON public.operator_assignments (tenant_id) WHERE unassigned_at IS NULL;

-- Deny-all RLS: no policies + FORCE → only the RLS-bypassing service role reaches it.
ALTER TABLE public.operator_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operator_assignments FORCE ROW LEVEL SECURITY;
