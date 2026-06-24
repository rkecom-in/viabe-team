/** VT-294 — Ops Console V2 Decision Audit (operator decision-audit/metrics panel). Inherits the VT-290 contract. */

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
    <main
      data-area="team-ops-behaviour"
      className="ops-behaviour min-h-screen space-y-6 bg-gray-50 p-6"
    >
      <header>
        <h1 className="text-2xl font-semibold text-gray-900">Decision Audit</h1>
        <p className="mt-1 text-sm text-gray-600">
          Operator decision metrics and recent decisions. Log feedback on a decision to record an
          audit note.
        </p>
      </header>
      {error ? (
        <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
          <p data-section-error className="text-sm text-red-700">
            couldn&apos;t load: {error}
          </p>
        </section>
      ) : (
        <BehaviourPanel metrics={metrics} decisions={decisions} />
      )}
    </main>
  )
}
