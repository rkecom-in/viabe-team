/**
 * VT-377 panel leg — the resolveAssignedTenants intersection the run-control page applies
 * before rendering tenant tiles. Extracted to its own dep-less module so the intersection
 * is unit-falsifiable in vitest WITHOUT importing the server component (page.tsx pulls
 * next/navigation + requireOpsOperator + the orchestrator client). The page imports + uses
 * this; the test imports only this.
 *
 * Semantics (mirroring lib/ops/assignments canAccessTenant, per tenant):
 *   - VTAdmin (assignedTenants = null) → ALL tenants, unscoped.
 *   - VTR (assignedTenants = string[]) → ONLY the intersection of the tenant list with the
 *     assigned set. An EMPTY assigned set ⇒ no tiles (fail-closed; a VTR with no
 *     assignments sees nothing).
 */

/** One tenant tile candidate (the fetchTopTenants row shape the page renders). */
export interface ScopeTenant {
  tenant_id: string
  business_name: string | null
}

export function scopeTenantsForOperator(
  tenants: ScopeTenant[],
  assignedTenants: string[] | null,
): ScopeTenant[] {
  if (assignedTenants === null) return tenants // VTAdmin — unscoped
  const allowed = new Set(assignedTenants)
  return tenants.filter((t) => allowed.has(t.tenant_id))
}
