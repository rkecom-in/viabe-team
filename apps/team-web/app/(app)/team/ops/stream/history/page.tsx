/** VT-201 PR-2 — historical Ops stream view (/team/ops/stream/history).
 *
 * VT-412 (PR-D): opened to scoped VTR operators. requireFazal() → requireOpsOperator().
 * The tenant tiles in the filter sidebar are scoped to the operator's assigned set
 * (scopeTenantsForOperator); the actual data read (/api/ops/history) enforces the same
 * scope server-side AND de-identifies rows for a VTR — the page-level scoping here is
 * display narrowing, not the security boundary (that lives in the route).
 */

import { redirect } from 'next/navigation'

import { StreamHistoryView } from '@/components/ops/stream-history-view'
import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchTopTenants } from '@/lib/ops/data-access'
import { scopeTenantsForOperator } from '@/app/(app)/team/ops/run-control/scope-tenants'

export const dynamic = 'force-dynamic'

interface OpsStreamHistoryPageProps {
  searchParams: Promise<{ date?: string }>
}

function todayIst(): string {
  const now = new Date()
  const ist = new Date(now.getTime() + (5 * 60 + 30) * 60_000)
  return ist.toISOString().slice(0, 10)
}

export default async function OpsStreamHistoryPage(props: OpsStreamHistoryPageProps) {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/stream/history')
    throw err
  }

  const sp = await props.searchParams
  const date = sp.date && /^\d{4}-\d{2}-\d{2}$/.test(sp.date) ? sp.date : todayIst()
  const top = await fetchTopTenants(20)
  // A VTR's filter sidebar shows ONLY its assigned tenants; VTAdmin (null) sees all.
  const tenants = scopeTenantsForOperator(
    top.map((t) => ({ tenant_id: t.tenant_id, business_name: t.business_name })),
    operator.assignedTenants,
  )

  return (
    <main
      className="ops-stream-history-page bg-gray-50 min-h-screen p-6 space-y-6"
      data-area="team-ops-stream-history"
    >
      <header>
        <h1 className="text-2xl font-semibold text-gray-900">
          Ops Console — Stream History
        </h1>
      </header>

      <StreamHistoryView
        initialDate={date}
        availableTenants={tenants.map((t) => ({
          tenant_id: t.tenant_id,
          business_name: t.business_name,
        }))}
      />
    </main>
  )
}
