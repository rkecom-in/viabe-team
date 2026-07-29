/**
 * VT-721 — the rolling 7-day plan (VTR console view).
 *
 * Renders the tenant's week-plan revision chain (newest first): actions with the §0.1d
 * hand-off triple + approval badges, and each revision's WHY-notes. Read-only. Guarded
 * per-page (requireOpsOperator) + tenant-scoped (canAccessTenant, deny render — VT-290
 * fail-closed). Reads tenant_week_plans via the service client with an explicit tenant
 * predicate (the activity-flow pattern).
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import { serverSecretClient } from '@/lib/supabase-client'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

interface PlanAction {
  key: string
  objective: string
  directive: string
  assigned_to: string
  expected_outcome?: string
  status: string
  source: string
  requires_approval?: boolean
}

interface PlanNote {
  action_key?: string
  change: string
  reason: string
}

interface WeekPlanRow {
  id: string
  plan_date: string
  horizon_start: string
  horizon_end: string
  actions: PlanAction[]
  revision_notes: PlanNote[]
  generated_by: string
  model_id: string | null
  created_at: string
}

const STATUS_TONE: Record<string, string> = {
  planned: 'bg-secondary text-secondary-foreground',
  in_flight: 'bg-amber-100 text-amber-900',
  done: 'bg-emerald-100 text-emerald-900',
  dropped: 'bg-muted text-muted-foreground line-through',
}

export default async function WeekPlanPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/week-plan`)
    }
    throw err
  }
  if (!canAccessTenant(operator.assignedTenants, tenantId)) {
    return (
      <main className="p-6">
        <p className="text-sm text-muted-foreground">Not authorized for this tenant.</p>
      </main>
    )
  }

  const client = serverSecretClient()
  const { data, error } = await client
    .from('tenant_week_plans')
    .select(
      'id, plan_date, horizon_start, horizon_end, actions, revision_notes, generated_by, model_id, created_at'
    )
    .eq('tenant_id', tenantId)
    .order('plan_date', { ascending: false })
    .limit(30)
  const plans = (data ?? []) as WeekPlanRow[]

  return (
    <main
      className="bg-background min-h-screen p-6 space-y-6"
      data-area="team-ops-week-plan"
      data-tenant-id={tenantId}
    >
      <header className="bg-card rounded-lg shadow-sm border border-border p-6 space-y-2">
        <h1 className="text-2xl font-semibold text-foreground">Rolling 7-day plan</h1>
        <p className="text-sm text-muted-foreground">
          tenant_id: <code className="font-mono text-xs text-foreground">{tenantId}</code>
          {' '}· revised daily · a planned action is an intention — effects still pass approvals
        </p>
        <p className="text-xs text-muted-foreground">
          <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
            ← tenant dashboard
          </a>
        </p>
      </header>

      {error ? (
        <p className="text-sm text-destructive">Failed to load plans: {error.message}</p>
      ) : plans.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No week plan yet — the daily revision pass hasn&apos;t produced one for this tenant
          (flag off, or the first fire is pending).
        </p>
      ) : (
        plans.map((plan, i) => (
          <section
            key={plan.id}
            className="bg-card rounded-lg shadow-sm border border-border p-6 space-y-4"
          >
            <div className="flex items-baseline justify-between flex-wrap gap-2">
              <h2 className="text-lg font-medium text-foreground">
                {plan.plan_date}
                {i === 0 ? (
                  <span className="ml-2 text-xs rounded-full bg-emerald-100 text-emerald-900 px-2 py-0.5 align-middle">
                    current
                  </span>
                ) : null}
              </h2>
              <p className="text-xs text-muted-foreground">
                horizon {plan.horizon_start} → {plan.horizon_end} · by {plan.generated_by}
                {plan.model_id ? ` (${plan.model_id})` : ''}
              </p>
            </div>

            <ol className="space-y-2">
              {plan.actions.map((a) => (
                <li
                  key={a.key}
                  className="rounded-md border border-border p-3 space-y-1 overflow-hidden"
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className={`text-[11px] uppercase tracking-wide rounded-full px-2 py-0.5 ${STATUS_TONE[a.status] ?? 'bg-secondary'}`}
                    >
                      {a.status}
                    </span>
                    <span className="text-sm font-medium text-foreground break-words">
                      {a.objective}
                    </span>
                    {a.requires_approval ? (
                      <span className="text-[11px] rounded-full bg-amber-100 text-amber-900 px-2 py-0.5">
                        needs owner approval
                      </span>
                    ) : null}
                  </div>
                  <p className="text-xs text-muted-foreground break-words">
                    → {a.assigned_to} · {a.directive}
                    {a.expected_outcome ? ` · expect: ${a.expected_outcome}` : ''}
                  </p>
                </li>
              ))}
            </ol>

            {plan.revision_notes.length > 0 ? (
              <div className="border-t border-border pt-3">
                <h3 className="text-xs uppercase tracking-wide text-muted-foreground mb-2">
                  Why this revision
                </h3>
                <ul className="space-y-1">
                  {plan.revision_notes.map((n, j) => (
                    <li key={j} className="text-xs text-muted-foreground break-words">
                      <span className="font-medium text-foreground">{n.change}</span>
                      {n.action_key ? ` ${n.action_key}` : ''} — {n.reason}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>
        ))
      )}
    </main>
  )
}
