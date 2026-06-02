/** VT-292 — Ops Console V2 Escalations queue. Inherits the VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { EscalationsList } from '@/components/ops/escalations-list'
import { fetchEscalations } from '@/lib/ops/escalations'
import type { MaskedOpsRow } from '@/lib/ops/de-identify'

export const dynamic = 'force-dynamic'

export default async function OpsEscalationsPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/escalations')
    throw err
  }

  let rows: MaskedOpsRow[] = []
  let error: string | null = null
  try {
    rows = await fetchEscalations(operator)
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsEscalationsPage: fetchEscalations failed', err)
  }

  return (
    <main data-area="team-ops-escalations" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">
          Escalations{operator.assignedTenants === null ? ' (all)' : ' (your businesses)'}
        </h1>
      </header>
      {error ? <p data-section-error>couldn&apos;t load: {error}</p> : <EscalationsList rows={rows} />}
    </main>
  )
}
