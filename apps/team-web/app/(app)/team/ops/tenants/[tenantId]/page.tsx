/**
 * Ops Console — per-tenant view (VT-405 Part A: the discovery panel the operator LANDS on).
 *
 * Replaces the VT-123 dashboard that gated with requireFazal() ONLY (no assignment scoping — a
 * cross-tenant exposure) and rendered unscoped, unstyled tables. Now: requireOpsOperator +
 * canAccessTenant (app leg) → vtrTenantProfile (endpoint gate + vtr_connection view, the PII-wall)
 * → TenantDiscoveryPanel (signup + auto-discovered draft + confirmation status, non-PII).
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import { TenantDiscoveryPanel } from '@/components/ops/tenant-discovery-panel'
import { vtrTenantProfile } from '@/lib/orchestrator-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

export default async function TenantDashboardPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}`)
    throw err
  }

  // App-leg scoping (VT-405 §1.4): a VTR may only view assigned tenants; VTAdmin (assignedTenants
  // null) passes. Defense-in-depth ABOVE the endpoint's require_vtr_action gate + the view's
  // app_vtr_operator() assignment predicate. Never trust a client scoping field (VT-293/294).
  if (!canAccessTenant(operator.assignedTenants, tenantId)) {
    return (
      <main
        className="ops-tenant min-h-screen bg-gray-50 p-6"
        data-area="team-ops-tenant"
        data-tenant-id={tenantId}
      >
        <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
          <p className="text-sm text-gray-600">You are not assigned to this tenant.</p>
        </section>
      </main>
    )
  }

  const { ok, profile, reason } = await vtrTenantProfile(operator.operatorId, tenantId)

  return (
    <main
      className="ops-tenant min-h-screen space-y-6 bg-gray-50 p-6"
      data-area="team-ops-tenant"
      data-tenant-id={tenantId}
    >
      {!ok || !profile ? (
        <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
          <p data-section-error className="text-sm text-red-700">
            {!ok ? `couldn't load this tenant: ${reason}` : 'tenant not found, or not visible to you'}
          </p>
        </section>
      ) : (
        <TenantDiscoveryPanel profile={profile} />
      )}
    </main>
  )
}
