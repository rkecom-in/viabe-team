/** VT-293 — Ops Console V2 Activity / Pipelines. Inherits the VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { ActivityStream } from '@/components/ops/activity-stream'
import { fetchActiveRuns, type ActivityRun } from '@/lib/ops/activity'

export const dynamic = 'force-dynamic'

export default async function OpsActivityPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/activity')
    throw err
  }

  let runs: ActivityRun[] = []
  let error: string | null = null
  try {
    runs = await fetchActiveRuns(operator)
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsActivityPage: fetchActiveRuns failed', err)
  }

  return (
    <main data-area="team-ops-activity" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">
          Activity{operator.assignedTenants === null ? ' (all)' : ' (your businesses)'}
        </h1>
      </header>
      {error ? <p data-section-error>couldn&apos;t load: {error}</p> : <ActivityStream runs={runs} />}
    </main>
  )
}
