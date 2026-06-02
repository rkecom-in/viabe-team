/** VT-291 — Ops Console V2 Fleet (agent health listing). Inherits the VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { FleetList } from '@/components/ops/fleet-list'
import { fetchFleet, type FleetRow } from '@/lib/ops/fleet'

export const dynamic = 'force-dynamic'

export default async function OpsFleetPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/fleet')
    throw err
  }

  let rows: FleetRow[] = []
  let error: string | null = null
  try {
    rows = await fetchFleet(operator)
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsFleetPage: fetchFleet failed', err)
  }

  return (
    <main data-area="team-ops-fleet" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">
          Fleet{operator.assignedTenants === null ? ' (all businesses)' : ' (your businesses)'}
        </h1>
      </header>
      {error ? (
        <p data-section-error>couldn&apos;t load fleet: {error}</p>
      ) : (
        <FleetList rows={rows} />
      )}
    </main>
  )
}
