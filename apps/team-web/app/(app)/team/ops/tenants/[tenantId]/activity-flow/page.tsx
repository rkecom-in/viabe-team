/**
 * VT-704 — Tenant ACTIVITY FLOW: the 30-day time-based conversation + decision + execution
 * flow (Fazal 2026-07-24, modeled on the e2e-sim flow diagram). Owner ↔ Manager turns render
 * as chat bubbles; the Manager's decide/act spine (tm_audit_log), sub-agent dispatches,
 * approvals, comms deliveries, step errors, incidents and alerts render as annotated system
 * cards on the center rail — one time-ordered story of what the Team Manager did, decided,
 * asked and conveyed for this business.
 *
 * Guarded per-page (requireOpsOperator) + tenant-scoped (canAccessTenant, deny render).
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import {
  fetchTenantFlow,
  groupByDay,
  type FlowEvent,
} from '@/lib/ops/activity-flow'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

const KIND_LABEL: Record<FlowEvent['kind'], string> = {
  message: 'message',
  decision: 'manager decision',
  task: 'sub-agent',
  step_error: 'execution error',
  approval: 'approval',
  comms: 'comms',
  incident: 'incident',
  alert: 'alert',
}

const SEV_STRIPE: Record<FlowEvent['severity'], string> = {
  info: 'border-l-gray-300',
  warn: 'border-l-amber-500',
  error: 'border-l-red-500',
}

function SystemCard({ e }: { e: FlowEvent }) {
  return (
    <div className={`mx-auto w-full max-w-2xl rounded-md border border-gray-200 bg-white border-l-4 ${SEV_STRIPE[e.severity]} px-4 py-2.5`}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[11px] uppercase tracking-wide text-gray-500 font-medium">
          {KIND_LABEL[e.kind]}
        </span>
        <span className="text-[11px] font-mono text-gray-400">{String(e.ts).slice(11, 19)}Z</span>
      </div>
      <p className="text-sm font-medium text-gray-900 mt-0.5">{e.title}</p>
      {e.body ? (
        <p className="text-xs text-gray-600 whitespace-pre-wrap mt-1">{e.body}</p>
      ) : null}
      {Object.entries(e.meta).filter(([, v]) => v).length > 0 ? (
        <p className="text-[11px] font-mono text-gray-400 mt-1">
          {Object.entries(e.meta)
            .filter(([, v]) => v)
            .map(([k, v]) => `${k}=${v}`)
            .join('  ')}
        </p>
      ) : null}
    </div>
  )
}

function MessageBubble({ e }: { e: FlowEvent }) {
  const owner = e.lane === 'owner'
  return (
    <div className={`flex ${owner ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[70%] rounded-lg px-3.5 py-2 text-sm ${
          owner
            ? 'bg-green-100 text-green-950 rounded-br-sm'
            : 'bg-white border border-gray-200 text-gray-900 rounded-bl-sm'
        }`}
      >
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] font-medium text-gray-500">{e.title}</span>
          <span className="text-[10px] font-mono text-gray-400">{String(e.ts).slice(11, 16)}Z</span>
        </div>
        <p className="whitespace-pre-wrap mt-0.5">{e.body}</p>
      </div>
    </div>
  )
}

export default async function TenantActivityFlowPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/activity-flow`)
    }
    throw err
  }

  if (!canAccessTenant(operator.assignedTenants, tenantId)) {
    return (
      <main className="bg-background min-h-screen p-6" data-area="team-ops-activity-flow">
        <p className="text-sm text-destructive">Not assigned to this tenant.</p>
      </main>
    )
  }

  const { events, counts } = await fetchTenantFlow(operator, tenantId)
  const days = groupByDay(events)
  const capped = Object.entries(counts).filter(([, c]) => c.fetched >= c.cap)

  return (
    <main
      className="bg-background min-h-screen p-6 space-y-6"
      data-area="team-ops-activity-flow"
      data-tenant-id={tenantId}
    >
      <header className="bg-card rounded-lg shadow-sm border border-border p-6 space-y-2">
        <h1 className="text-2xl font-semibold text-foreground">Activity flow — last 30 days</h1>
        <p className="text-sm text-muted-foreground">
          tenant_id: <code className="font-mono text-xs text-foreground">{tenantId}</code>
          {' '}| {events.length} events
        </p>
        <p className="text-xs text-muted-foreground">
          <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
            ← tenant dashboard
          </a>{' '}
          |{' '}
          <a className="underline" href={`/team/ops/tenants/${tenantId}/plan`}>
            plan →
          </a>
        </p>
        {capped.length > 0 ? (
          <p className="text-xs text-amber-700" data-flow-truncation>
            Truncated sources (showing newest rows only):{' '}
            {capped.map(([name, c]) => `${name} (${c.cap})`).join(', ')}
          </p>
        ) : null}
      </header>

      {events.length === 0 ? (
        <p className="text-sm text-muted-foreground">No activity recorded in the last 30 days.</p>
      ) : (
        days.map(({ day, events: dayEvents }) => (
          <section key={day} className="space-y-3" data-flow-day={day}>
            <h2 className="sticky top-0 z-10 mx-auto w-fit rounded-full bg-gray-800 text-white text-xs px-4 py-1 font-medium">
              {day}
            </h2>
            {dayEvents.map((e, i) =>
              e.kind === 'message' ? (
                <MessageBubble key={`${day}-${i}`} e={e} />
              ) : (
                <SystemCard key={`${day}-${i}`} e={e} />
              ),
            )}
          </section>
        ))
      )}
    </main>
  )
}
