/**
 * VT-290 — VTR↔business assignment scoping (operator_assignments, migration 072).
 *
 * The table is deny-all RLS (service-role only). VTR scoping is enforced HERE, app-side:
 * a VTR's queries are filtered to their ACTIVE assigned tenant set; VTAdmin is unscoped.
 * Fail-CLOSED: a VTR with no assignments gets an empty set (sees nothing). Reassignment
 * takes effect immediately (no client cache — resolved per request from the table).
 *
 * VT-380 (B3): serverSecretClient() is resolved LAZILY — only after the VTADMIN
 * early-return — so a credless env (SUPABASE_URL/SUPABASE_SECRET_KEY unset) never
 * throws for VTAdmin callers. Aligns with requireOpsOperator's catch semantics:
 * degrade gracefully rather than 500 before the role check.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'

type Client = { from: (t: string) => any }

/** Active tenant_ids assigned to a VTR. VTAdmin → null (means "all", unscoped). Empty
 *  array for a VTR = fail-closed (no access).
 *
 *  @param clientFactory  Optional override — resolved LAZILY (only when role is VTR
 *                        and operatorId is non-empty). Pass a factory, not a client
 *                        instance, to preserve lazy semantics in callers. If omitted,
 *                        defaults to serverSecretClient(). */
export async function resolveAssignedTenants(
  operatorId: string,
  role: OperatorRole,
  clientFactory?: () => Client,
): Promise<string[] | null> {
  if (role === OperatorRole.VTADMIN) return null // unscoped — all tenants
  if (!operatorId) return [] // fail-closed

  // Lazy: only call (and throw) serverSecretClient() AFTER the early-returns above.
  // A credless env degrades cleanly for VTAdmin callers instead of hard-500ing.
  let client: Client
  try {
    client = clientFactory ? clientFactory() : serverSecretClient()
  } catch {
    // Credentials missing — fail-CLOSED (VTR sees nothing) rather than hard-500.
    console.error('resolveAssignedTenants: client init failed; failing closed (credless env?)')
    return []
  }

  const { data, error } = await client
    .from('operator_assignments')
    .select('tenant_id')
    .eq('operator_id', operatorId)
    .is('unassigned_at', null)
  if (error) {
    // fail-CLOSED on error: a VTR sees nothing rather than everything.
    console.error('resolveAssignedTenants: query failed; failing closed', error)
    return []
  }
  return (data ?? []).map((r: { tenant_id: string }) => r.tenant_id)
}

/** True if this operator may act on a given tenant. VTAdmin always; VTR iff assigned. */
export function canAccessTenant(
  assignedTenants: string[] | null,
  tenantId: string,
): boolean {
  if (assignedTenants === null) return true // VTAdmin
  return assignedTenants.includes(tenantId)
}
