/**
 * Ops Console — per-tenant view (VT-405 Part A: the discovery panel the operator LANDS on).
 *
 * Replaces the VT-123 dashboard that gated with requireFazal() ONLY (no assignment scoping — a
 * cross-tenant exposure) and rendered unscoped, unstyled tables. Now: requireOpsOperator +
 * canAccessTenant (app leg) → vtrTenantProfile (endpoint gate + vtr_connection view, the PII-wall)
 * → TenantDiscoveryPanel (signup + auto-discovered draft + confirmation status, non-PII).
 */

import { redirect } from 'next/navigation'

import { issueOperatorJwt, OPERATOR_STREAM_TTL_SEC } from '@/lib/auth/operator-jwt'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import { TenantDiscoveryPanel } from '@/components/ops/tenant-discovery-panel'
import { TmActivityFeed } from '@/components/ops/tm-activity-feed'
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
        className="ops-tenant min-h-screen bg-background p-6"
        data-area="team-ops-tenant"
        data-tenant-id={tenantId}
      >
        <section className="rounded-lg border border-border bg-card p-6 shadow-sm">
          <p className="text-sm text-muted-foreground">You are not assigned to this tenant.</p>
        </section>
      </main>
    )
  }

  const { ok, profile, reason } = await vtrTenantProfile(operator.operatorId, tenantId)

  // Mint a short-lived operator JWT for the browser Realtime subscription — only
  // on the success path so error responses skip the ~50ms issue cost.
  const operatorJwt =
    ok && profile
      ? await issueOperatorJwt(operator.operatorId, { ttlSec: OPERATOR_STREAM_TTL_SEC })
      : null

  return (
    <main
      className="ops-tenant min-h-screen space-y-6 bg-background p-6"
      data-area="team-ops-tenant"
      data-tenant-id={tenantId}
    >
      {!ok || !profile ? (
        <section className="rounded-lg border border-border bg-card p-6 shadow-sm">
          <p data-section-error className="text-sm text-destructive">
            {!ok ? `couldn't load this tenant: ${reason}` : 'tenant not found, or not visible to you'}
          </p>
        </section>
      ) : (
        <TenantDiscoveryPanel profile={profile} />
      )}
      {ok && profile && operatorJwt ? (
        <TmActivityFeed tenantId={tenantId} operatorJwt={operatorJwt} />
      ) : null}
    </main>
  )
}
