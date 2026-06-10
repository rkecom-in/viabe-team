/**
 * VT-370 Gap-6 — Tenant Agents (VTR agent-correction surface).
 *
 * Reads per-agent autonomy (vtr_agent_autonomy view — NO revoke_reason by construction) and
 * draft batches (vtr_draft_batches view — aggregates only: counts + template-name enums; never
 * params/owner_feedback/customer_id) via the orchestrator. operator_id is server-derived from
 * the session claim (requireOpsOperator), never client input.
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { AgentsPanel } from '@/components/ops/agents-panel'
import { vtrAgentState, vtrDraftBatches } from '@/lib/orchestrator-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

export default async function TenantAgentsPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/agents`)
    }
    throw err
  }

  const [agentState, batches] = await Promise.all([
    vtrAgentState(operator.operatorId, tenantId),
    vtrDraftBatches(operator.operatorId, tenantId),
  ])

  return (
    <main
      className="ops-tenant-agents bg-gray-50 min-h-screen p-6 space-y-6"
      data-area="team-ops-tenant-agents"
      data-tenant-id={tenantId}
    >
      <header className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-2">
        <h1 className="text-2xl font-semibold text-gray-900">Agents — autonomy &amp; batches</h1>
        <p className="text-sm text-gray-600">
          tenant_id: <code className="font-mono text-xs text-gray-700">{tenantId}</code>
        </p>
        <p className="text-xs text-gray-500">
          <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
            ← tenant dashboard
          </a>{' '}
          |{' '}
          <a className="underline" href={`/team/ops/tenants/${tenantId}/plan`}>
            plan →
          </a>
        </p>
      </header>

      {!agentState.ok && (
        <p data-section-error className="text-sm text-red-700">
          couldn&apos;t load agent state: {agentState.reason}
        </p>
      )}
      {!batches.ok && (
        <p data-section-error className="text-sm text-red-700">
          couldn&apos;t load draft batches: {batches.reason}
        </p>
      )}
      <AgentsPanel tenantId={tenantId} agents={agentState.agents} batches={batches.rows} />
    </main>
  )
}
