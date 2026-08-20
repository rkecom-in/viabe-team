/**
 * VT-733 slice A — cost-console access rules, dep-less so they are unit-falsifiable.
 *
 * Fazal 2026-08-05: "The VTR and the VTRAdmin (me) should be able to have control over the prices
 * being spent behind a particular tenant." Two different verbs in one sentence — SEE and CONTROL —
 * and they land on different roles, so they are separated here rather than inside a route handler.
 *
 * What I verified before writing this (deployed dev, 2026-08-05), because the row's premise was that
 * the split already existed and only needed surfacing:
 *   - The DATABASE has no VTR/VTAdmin write distinction at all: both caps tables are
 *     rls_on + rls_forced with SELECT-only policies for {public} and NO write policy, so writes are
 *     denied to every non-BYPASSRLS role. `budget_gate.py`'s "only VTR admin can set/control limits"
 *     is a docstring claim, not an enforced boundary.
 *   - The WEB layer already carries the role machinery (requireOpsOperator + assignedTenants), which
 *     is what the run-replay cluster scopes on.
 * So slice A wires cap WRITES into the existing web-layer role gate — it does not invent a role
 * system, and it does not pretend the database is enforcing something it is not.
 *
 * IDOR rule (VT-293/294, caught twice): the tenant set a query runs over is ALWAYS derived from the
 * operator's own assignment, never from a client parameter. `scopeCostTenantFilter` is the only way
 * a route should produce that set.
 */

import { canAccessTenant } from '@/lib/ops/assignments'

/** VTAdmin/Fazal carry `assignedTenants === null` (unscoped). A VTR carries its assigned array. */
export function hasFullCostAccess(assignedTenants: string[] | null): boolean {
  return assignedTenants === null
}

/**
 * May this operator CHANGE a spend cap?
 *
 * VTAdmin/Fazal only. A VTR sees its tenants' spend and cap status but cannot raise or lower a
 * ceiling: a cap is the company's exposure decision, and the operator closest to a tenant is the one
 * most likely to be asked to lift it "just for today". Read and write deliberately diverge.
 */
export function canEditCaps(assignedTenants: string[] | null): boolean {
  return hasFullCostAccess(assignedTenants)
}

/** May this operator edit the PLATFORM-wide cap? Same answer, named separately so a future
 *  "VTAdmin may set tenant caps but only Fazal sets the platform ceiling" split has a seam. */
export function canEditPlatformCaps(assignedTenants: string[] | null): boolean {
  return hasFullCostAccess(assignedTenants)
}

export interface ScopedCostFilter {
  /** null = every tenant (VTAdmin/Fazal). An array = exactly the tenants this operator may see. */
  tenantIds: string[] | null
  /** True when the caller asked for tenants it may not see — the route returns 403 rather than
   *  silently narrowing, so a mis-scoped console surfaces instead of quietly showing less. */
  denied: boolean
}

/**
 * Narrow a requested tenant filter to what this operator may actually read.
 *
 * - VTAdmin/Fazal: the request passes through (an explicit list stays a list; absent = all).
 * - VTR with no request: the WHOLE assigned set — never "all".
 * - VTR requesting tenants outside its set: `denied`, not a silent intersection.
 * - VTR with an empty assignment: `[]`, which callers must treat as "no rows" and never as "all".
 */
export function scopeCostTenantFilter(
  assignedTenants: string[] | null,
  requested: string[] | undefined,
): ScopedCostFilter {
  if (hasFullCostAccess(assignedTenants)) {
    return { tenantIds: requested && requested.length > 0 ? requested : null, denied: false }
  }
  const assigned = assignedTenants ?? []
  if (!requested || requested.length === 0) {
    return { tenantIds: assigned, denied: false }
  }
  const outOfScope = requested.filter((t) => !canAccessTenant(assignedTenants, t))
  if (outOfScope.length > 0) return { tenantIds: [], denied: true }
  return { tenantIds: requested, denied: false }
}

/** A cap ceiling as the console reports it. `null` means NO ceiling is configured — which is the
 *  live state on dev as of 2026-08-05 and must never render as "0% used". */
export type CapState = 'none' | 'ok' | 'soft' | 'hard'

/**
 * The share of a ceiling consumed, or null when there is no ceiling.
 *
 * Returning null rather than 0 is the point: a missing cap and an unused cap look identical on a
 * progress bar, and only one of them means "this tenant can spend without limit".
 */
export function capUtilisation(spend: number, ceiling: number | null): number | null {
  if (ceiling === null || ceiling <= 0) return null
  return spend / ceiling
}
