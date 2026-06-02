/** VT-294 — Ops Console V2 Behaviour & Training. Inherits the VT-290 contract. */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { BehaviourPanel } from '@/components/ops/behaviour-panel'
import {
  fetchDecisionMetrics,
  fetchRecentDecisions,
  type DecisionMetrics,
  type DecisionRow,
} from '@/lib/ops/behaviour'

export const dynamic = 'force-dynamic'

const EMPTY: DecisionMetrics = { scope: 'own', total: 0, byAction: {} }

export default async function OpsBehaviourPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/behaviour')
    throw err
  }

  let metrics: DecisionMetrics = EMPTY
  let decisions: DecisionRow[] = []
  let error: string | null = null
  try {
    ;[metrics, decisions] = await Promise.all([
      fetchDecisionMetrics(operator),
      fetchRecentDecisions(operator),
    ])
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsBehaviourPage: load failed', err)
  }

  return (
    <main data-area="team-ops-behaviour" className="p-6 space-y-4">
      <header>
        <h1 className="text-2xl font-semibold">Behaviour &amp; Training</h1>
      </header>
      {error ? <p data-section-error>couldn&apos;t load: {error}</p> : <BehaviourPanel metrics={metrics} decisions={decisions} />}
    </main>
  )
}
