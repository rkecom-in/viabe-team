/**
 * VT-370 Gap-6 — Tenant Plan (VTR plan-editing surface).
 *
 * Reads the latest plan + metadata history via the orchestrator's vtr-plan endpoint
 * (vtr_business_plan / vtr_plan_history views inside vtr_connection — never raw tables).
 * operator_id is server-derived from the session claim (requireOpsOperator), never client input.
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { PlanBoard } from '@/components/ops/plan-board'
import { vtrPlan } from '@/lib/orchestrator-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

export default async function TenantPlanPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/plan`)
    }
    throw err
  }

  const { ok, plan, history, reason } = await vtrPlan(operator.operatorId, tenantId)

  return (
    <main
      className="ops-tenant-plan bg-background min-h-screen p-6 space-y-6"
      data-area="team-ops-tenant-plan"
      data-tenant-id={tenantId}
    >
      <header className="bg-card rounded-lg shadow-sm border border-border p-6 space-y-2">
        <h1 className="text-2xl font-semibold text-foreground">Business plan</h1>
        <p className="text-sm text-muted-foreground">
          tenant_id: <code className="font-mono text-xs text-foreground">{tenantId}</code>
          {plan ? ` | version ${plan.version} | by ${plan.generated_by}` : ''}
        </p>
        <p className="text-xs text-muted-foreground">
          <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
            ← tenant dashboard
          </a>{' '}
          |{' '}
          <a className="underline" href={`/team/ops/tenants/${tenantId}/agents`}>
            agents →
          </a>
        </p>
      </header>

      {!ok ? (
        <p data-section-error className="text-sm text-destructive">
          couldn&apos;t load the plan: {reason}
        </p>
      ) : (
        <PlanBoard tenantId={tenantId} plan={plan} history={history} />
      )}
    </main>
  )
}
