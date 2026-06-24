/**
 * VT-412 — scoped, de-identified "my assigned tenants" index.
 *
 * Backs the ops tenants-index page (the browse-list the audit found missing: operators could
 * only reach tenants/[tenantId] via deep-links). Scoping reuses the VT-290 contract EXACTLY as
 * lib/ops/fleet.ts does — the tenant set is derived SERVER-SIDE from the operator's assignment
 * (operator.assignedTenants), NEVER from a client field (IDOR rule, VT-293/294):
 *   - VTR  → only ACTIVE assigned tenant_ids (.in(...)); empty assignment = fail-closed [].
 *   - VTAdmin (assignedTenants === null) → all tenants (unscoped).
 *
 * De-identified, business-level ONLY (CL-390 / CL-425 PII boundary): business_name +
 * verification_status + phase + plan_tier + created_at. NO customer PII (no owner name, no
 * WhatsApp number, no GSTIN) ever leaves this query. tenants is FORCE-RLS service-role-only, so
 * the read goes through serverSecretClient() with the app-side assignment predicate as the gate.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'

export interface TenantIndexRow {
  tenant_id: string
  business_name: string | null
  /** unverified | gstin_verified | vtr_verified (migration 120). Business-level, non-PII. */
  verification_status: string | null
  phase: string | null
  plan_tier: string | null
  /** when the tenant row was created (business-level timestamp, not customer activity). */
  created_at: string | null
}

interface OpsOperatorLike {
  role: OperatorRole
  assignedTenants: string[] | null
}

type Client = { from: (t: string) => any }

/**
 * The operator's tenants, de-identified + assignment-scoped. Sorted by business_name.
 * Fail-CLOSED: a VTR with no assignments sees nothing.
 */
export async function fetchAssignedTenants(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
): Promise<TenantIndexRow[]> {
  const { assignedTenants } = operator
  // fail-CLOSED: a VTR with no assignments sees nothing (mirrors fleet.ts).
  if (assignedTenants !== null && assignedTenants.length === 0) return []

  let q = client
    .from('tenants')
    .select('id, business_name, verification_status, phase, plan_tier, created_at')
    .order('business_name', { ascending: true })
  // Scope to the operator's assigned set server-side; VTAdmin (null) stays unscoped.
  if (assignedTenants !== null) q = q.in('id', assignedTenants)

  const { data, error } = await q
  if (error) {
    // fail-CLOSED on error: an operator sees an empty index rather than an unscoped read.
    console.error('fetchAssignedTenants: query failed; failing closed', error)
    return []
  }

  return ((data ?? []) as {
    id: string
    business_name: string | null
    verification_status: string | null
    phase: string | null
    plan_tier: string | null
    created_at: string | null
  }[]).map((t) => ({
    tenant_id: String(t.id),
    business_name: t.business_name ?? null,
    verification_status: t.verification_status ?? null,
    phase: t.phase ?? null,
    plan_tier: t.plan_tier ?? null,
    created_at: t.created_at ?? null,
  }))
}
