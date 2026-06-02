/** VT-296 — Ops Console V2 Monitoring / Watchdog board. Inherits the VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { MonitoringBoard } from '@/components/ops/monitoring-board'
import { fetchMonitoringBoard, type MonitoringItem } from '@/lib/ops/monitoring'

export const dynamic = 'force-dynamic'

export default async function OpsMonitoringPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/monitoring')
    throw err
  }

  let items: MonitoringItem[] = []
  let error: string | null = null
  try {
    items = await fetchMonitoringBoard(operator)
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsMonitoringPage: fetchMonitoringBoard failed', err)
  }

  return (
    <main data-area="team-ops-monitoring" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">
          Monitoring{operator.assignedTenants === null ? ' (all)' : ' (your businesses)'}
        </h1>
      </header>
      {error ? <p data-section-error>couldn&apos;t load: {error}</p> : <MonitoringBoard items={items} />}
    </main>
  )
}
